"""InferenceNetwork class for approximate posteriors"""

import numpy as np
import tensorflow as tf
from netlds.chol_utils import blk_tridiag_chol, blk_chol_inv


class InferenceNetwork(object):
    """Base class for inference networks"""

    def __init__(
            self, dim_input=None, dim_latent=None, rng=0, dtype=tf.float32):
        """
        Set base class attributes

        Args:
            dim_input (int): dimension of inputs that are transformed by a
                neural network to provide data-point specific distributional
                parameters of the approximate posterior
            dim_latent (int): dimension of latent state
            rng (int): rng seed for generating samples of observations
            dtype (tf.Dtype): data type for all model variables, placeholders

        """

        # set basic dims
        self.dim_input = dim_input
        self.dim_latent = dim_latent
        # set rng seed for drawing samples of dynamic trajectories
        self.rng = rng
        # # create list of input placeholders
        # self.input_ph = None
        # use same data type throughout graph construction
        self.dtype = dtype

    def build_graph(self, *args, **kwargs):
        """Build tensorflow computation graph for inference network"""
        raise NotImplementedError

    def evaluate_entropy(self):
        """Entropy of approximate posterior"""
        raise NotImplementedError

    def generate_samples(self, sess, num_samples):
        """Draw samples from approximate posterior"""
        raise NotImplementedError


class SmoothingLDS(InferenceNetwork):
    """
    Approximate posterior is modeled as a Gaussian distribution with a
    structure mirroring that from a linear dynamical system
    """

    def __init__(
            self, dim_input=None, dim_latent=None, rng=0, dtype=tf.float32,
            num_time_pts=None):

        super(SmoothingLDS, self).__init__(
            dim_input=dim_input, dim_latent=dim_latent, rng=rng,
            dtype=dtype)

        self.num_time_pts = num_time_pts

    def build_graph(self, *args):
        """Build tensorflow computation graph for inference network"""

        # initialize variables that are *not* shared with generative model
        with tf.variable_scope('model_params'):
            self._initialize_model_vars()

        # should eventually become user options
        tr_norm_initializer = tf.initializers.truncated_normal(
            mean=0.0, stddev=0.1, dtype=self.dtype)
        zeros_initializer = tf.initializers.zeros(dtype=self.dtype)
        activation = tf.nn.tanh
        use_bias = True
        kernel_initializer = tr_norm_initializer
        bias_initializer = zeros_initializer
        kernel_regularizer = None
        bias_regularizer = None
        num_layers = 2

        # construct data pipeline
        with tf.variable_scope('inference_input'):
            self.input_ph = tf.placeholder(
                dtype=self.dtype,
                shape=[None, self.num_time_pts, self.dim_input],
                name='obs_in_ph')
            self.samples_z = tf.random_normal(
                shape=[tf.shape(self.input_ph)[0],
                       self.num_time_pts,
                       self.dim_latent],
                mean=0.0, stddev=1.0, dtype=self.dtype, name='samples_z')

        with tf.variable_scope('inference_mlp'):
            # store layers in a list
            self.layers = []
            for l in range(num_layers):
                self.layers.append(tf.layers.Dense(
                    units=30,
                    activation=activation,
                    use_bias=use_bias,
                    kernel_initializer=kernel_initializer,
                    bias_initializer=bias_initializer,
                    kernel_regularizer=kernel_regularizer,
                    bias_regularizer=bias_regularizer,
                    name='layer_%02i' % l))

            self.layer_z_mean = tf.layers.Dense(
                units=self.dim_latent,
                activation=None,
                use_bias=use_bias,
                kernel_initializer=kernel_initializer,
                bias_initializer=bias_initializer,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                name='layer_z_mean')
            self.layer_z_vars = tf.layers.Dense(
                units=self.dim_latent * self.dim_latent,
                activation=None,
                use_bias=use_bias,
                kernel_initializer=kernel_initializer,
                bias_initializer=bias_initializer,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                name='layer_z_vars')

            # compute layer outputs from inference network input
            layer_input = self.input_ph
            for l in range(num_layers):
                layer_input = self.layers[l].apply(layer_input)
            self.hidden_act = layer_input

            # get data-dependent mean
            self.m_psi = self.layer_z_mean.apply(self.hidden_act)

            # get sqrt of inverse of data-dependent covariances
            r_psi_sqrt = self.layer_z_vars.apply(self.hidden_act)
            self.r_psi_sqrt = tf.reshape(
                r_psi_sqrt,
                [-1, self.num_time_pts, self.dim_latent, self.dim_latent])

        with tf.variable_scope('precision_matrix'):
            # get inverse of data-dependent covariances
            self.c_psi_inv = tf.matmul(
                self.r_psi_sqrt,
                tf.transpose(self.r_psi_sqrt, perm=[0, 1, 3, 2]),
                name='precision_diag_data_dep')

            self.AQ0invA_Qinv = tf.matmul(
                tf.matmul(self.A, self.Q0inv), self.A, transpose_b=True) \
                + self.Qinv
            self.AQinvA_Qinv = tf.matmul(
                tf.matmul(self.A, self.Qinv), self.A, transpose_b=True) \
                + self.Qinv
            self.AQ0inv = tf.matmul(-self.A, self.Q0inv)
            self.AQinv = tf.matmul(-self.A, self.Qinv)

            # put together components of precision matrix Sinv in tensor of
            # shape [num_samples, num_time_pts, dim_latent, dim_latent]
            Sinv_diag = tf.tile(
                tf.expand_dims(self.AQinvA_Qinv, 0),
                [self.num_time_pts - 2, 1, 1])
            Sinv_diag = tf.concat(
                [tf.expand_dims(self.Q0inv, 0),
                 tf.expand_dims(self.AQ0invA_Qinv, 0),
                 Sinv_diag], axis=0, name='precision_diag_static')
            self.Sinv_diag = tf.add(Sinv_diag, self.c_psi_inv,
                                    name='precision_diag')

            Sinv_ldiag = tf.tile(
                tf.expand_dims(self.AQinv, 0),
                [self.num_time_pts - 2, 1, 1], name='precision_lower_diag')
            Sinv_ldiag0 = tf.concat(
                [tf.expand_dims(self.AQ0inv, 0), Sinv_ldiag], axis=0)

            # we now have Sinv (represented as diagonal and off-diagonal
            # blocks); to sample from the posterior we need the square root
            # of the inverse of Sinv; fortunately this is fast given the
            # tridiagonal block structure of Sinv. First we'll compute the
            # Cholesky decomposition of Sinv, then calculate the inverse using
            # that decomposition

            # get cholesky decomposition for each element in batch
            def scan_chol(_, inputs):
                """inputs refer to diagonal blocks, outputs the L/U matrices"""
                chol_decomp_Sinv = blk_tridiag_chol(inputs, Sinv_ldiag0)
                return chol_decomp_Sinv

            # self.chol_decomp_Sinv = tf.scan(
            #     fn=scan_chol, elems=[self.Sinv_diag, Sinv_ldiag1])
            self.chol_decomp_Sinv = tf.scan(
                fn=scan_chol, elems=self.Sinv_diag,
                initializer=[Sinv_diag, Sinv_ldiag0],  # throwaway to get scan
                name='precision_chol_decomp')          # to behave

        # posterior mean
        with tf.variable_scope('posterior_mean'):
            ia = tf.reduce_sum(
                tf.multiply(self.c_psi_inv,
                            tf.expand_dims(self.m_psi, axis=2)),
                axis=3)
            # ia now S x T x dim_latent

            # get sample for each element in batch
            def scan_chol_inv(_, inputs):
                """inputs refer to L/U matrices, outputs to means"""
                [chol_decomp_Sinv_0, chol_decomp_Sinv_1, ia] = inputs
                # mult by R
                ib = blk_chol_inv(
                    chol_decomp_Sinv_0, chol_decomp_Sinv_1, ia,
                    lower=True, transpose=False)
                post_z_means = blk_chol_inv(
                    chol_decomp_Sinv_0, chol_decomp_Sinv_1, ib,
                    lower=False, transpose=True)

                return post_z_means

            self.post_z_means = tf.scan(
                fn=scan_chol_inv,
                elems=[self.chol_decomp_Sinv[0], self.chol_decomp_Sinv[1], ia],
                initializer=ia[0])  # throwaway to get scan to behave

        # sample from posterior
        with tf.variable_scope('posterior_samples'):

            def scan_chol_half_inv(_, inputs):
                """inputs refer to L/U matrices, outputs to means"""
                [chol_decomp_Sinv_0, chol_decomp_Sinv_1, samples] = inputs
                rands = blk_chol_inv(
                    chol_decomp_Sinv_0, chol_decomp_Sinv_1, samples,
                    lower=False, transpose=True)

                return rands

            rands = tf.scan(fn=scan_chol_half_inv,
                            elems=[self.chol_decomp_Sinv[0],
                                   self.chol_decomp_Sinv[1],
                                   self.samples_z],
                            initializer=ia[0])  # throwaway for scan to behave

            self.post_z_samples = self.post_z_means + rands

    def _initialize_model_vars(self):
        """Initialize variables of model prior"""

        tr_norm_initializer = tf.initializers.truncated_normal(
            mean=0.0, stddev=0.1, dtype=self.dtype)
        zeros_initializer = tf.initializers.zeros(dtype=self.dtype)

        # lazy for now; might want to have this as an option in the future
        self.gen_params = {}

        # mean of initial latent state
        if 'z0_mean' in self.gen_params:
            self.z0_mean = tf.get_variable(
                'z0_mean',
                initializer=self.gen_params['z0_mean'],
                dtype=self.dtype)
        else:
            self.z0_mean = tf.get_variable(
                'z0_mean',
                shape=[1, self.dim_latent],
                initializer=zeros_initializer,
                dtype=self.dtype)

        # means of transition matrix
        if 'A' in self.gen_params:
            self.A = tf.get_variable(
                'A',
                initializer=self.gen_params['A'],
                dtype=self.dtype)
        else:
            self.A = tf.get_variable(
                'A',
                initializer=0.5 * np.eye(self.dim_latent,
                                         dtype=self.dtype.as_numpy_dtype()),
                dtype=self.dtype)

        # square root of the innovation precision matrix
        if 'Q_sqrt' in self.gen_params:
            self.Q_sqrt = tf.get_variable(
                'Q_sqrt',
                initializer=self.gen_params['Q_sqrt'],
                dtype=self.dtype)
        else:
            self.Q_sqrt = tf.get_variable(
                'Q_sqrt',
                initializer=np.eye(
                    self.dim_latent,
                    dtype=self.dtype.as_numpy_dtype()),
                dtype=self.dtype)

        # square root of the initial innovation precision matrix
        if 'Q0_sqrt' in self.gen_params:
            self.Q0_sqrt = tf.get_variable(
                'Q0_sqrt',
                initializer=self.gen_params['Q0_sqrt'],
                dtype=self.dtype)
        else:
            self.Q0_sqrt = tf.get_variable(
                'Q0_sqrt',
                initializer=np.eye(
                    self.dim_latent,
                    dtype=self.dtype.as_numpy_dtype()),
                dtype=self.dtype)

        self.Q0 = tf.matmul(
            self.Q0_sqrt, self.Q0_sqrt, transpose_b=True, name='Q0')
        self.Q = tf.matmul(
            self.Q_sqrt, self.Q_sqrt, transpose_b=True, name='Q')
        self.Q0inv = tf.matrix_inverse(self.Q0, name='Q0inv')
        self.Qinv = tf.matrix_inverse(self.Q, name='Qinv')

    def evaluate_entropy(self):
        """Entropy of approximate posterior"""

        # determinant of the covariance is the square of the determinant of the
        # cholesky factor; determinant of the cholesky factor is the product of
        # the diagonal elements of the block-diagonal
        ln_det = -2.0 * tf.reduce_sum(
            tf.reduce_mean(
                tf.log(tf.matrix_diag_part(self.chol_decomp_Sinv[0])), axis=0))

        entropy = ln_det / 2.0 + self.dim_latent * self.num_time_pts / 2.0 * (
                    1.0 + np.log(2.0 * np.pi))

        return entropy

    def generate_samples(self, sess, observations):
        """
        Draw samples from approximate posterior

        Args:
            sess (tf.Session object)
            observations (num_samples x num_time_pts x num_inputs numpy array)

        Returns:
            num_samples x num_time_pts x dim_latent numpy array

        """

        feed_dict = {self.input_ph: observations}

        return sess.run(self.post_z_samples, feed_dict=feed_dict)

    def get_params(self, sess):
        """Get parameters of generative model"""

        A, z0_mean, Q_sqrt, Q, Q0_sqrt, Q0 = sess.run(
            [self.A, self.z0_mean, self.Q_sqrt, self.Q, self.Q0_sqrt, self.Q0])

        param_dict = {'A': A, 'z0_mean': z0_mean, 'Q': Q, 'Q0': Q0,
                      'Q_sqrt': Q_sqrt, 'Q0_sqrt': Q0_sqrt}

        return param_dict

    def get_posterior_means(self, sess, observations):
        """Get posterior means conditioned on observations"""

        feed_dict = {self.input_ph: observations}

        return sess.run(self.post_z_means, feed_dict=feed_dict)


class SmoothingLDSCoupled(SmoothingLDS):
    """
    Approximate posterior is modeled as a Gaussian distribution with a
    structure mirroring that from a linear dynamical system; parameters are
    coupled to parameters of the LDSCoupled GenerativeModel class through the
    use of the LDSCoupledModel Model class
    """

    def __init__(
            self, dim_input=None, dim_latent=None, rng=0, dtype=tf.float32,
            num_time_pts=None):

        super(SmoothingLDSCoupled, self).__init__(
            dim_input=dim_input, dim_latent=dim_latent, rng=rng,
            dtype=dtype, num_time_pts=num_time_pts)

    def build_graph(self, z0_mean, A, Q_sqrt, Q, Qinv, Q0_sqrt, Q0, Q0inv):
        """Build tensorflow computation graph for inference network"""

        # make variables shared with generative model attributes
        self.z0_mean = z0_mean
        self.A = A
        self.Q0_sqrt = Q0_sqrt
        self.Q_sqrt = Q_sqrt
        self.Q0 = Q0
        self.Q = Q
        self.Q0inv = Q0inv
        self.Qinv = Qinv

        # should eventually become user options
        tr_norm_initializer = tf.initializers.truncated_normal(
            mean=0.0, stddev=0.1, dtype=self.dtype)
        zeros_initializer = tf.initializers.zeros(dtype=self.dtype)
        activation = tf.nn.tanh
        use_bias = True
        kernel_initializer = tr_norm_initializer
        bias_initializer = zeros_initializer
        kernel_regularizer = None
        bias_regularizer = None
        num_layers = 2

        # construct data pipeline
        with tf.variable_scope('inference_input'):
            self.input_ph = tf.placeholder(
                dtype=self.dtype,
                shape=[None, self.num_time_pts, self.dim_input],
                name='obs_in_ph')
            self.num_samples_ph = tf.placeholder(
                shape=None, name='num_samples_ph', dtype=tf.int32)
            self.samples_z = tf.random_normal(
                shape=[tf.shape(self.input_ph)[0],
                       self.num_time_pts,
                       self.dim_latent],
                mean=0.0, stddev=1.0, dtype=self.dtype, name='samples_z')

        with tf.variable_scope('inference_mlp'):
            # store layers in a list
            self.layers = []
            for l in range(num_layers):
                self.layers.append(tf.layers.Dense(
                    units=30,
                    activation=activation,
                    use_bias=use_bias,
                    kernel_initializer=kernel_initializer,
                    bias_initializer=bias_initializer,
                    kernel_regularizer=kernel_regularizer,
                    bias_regularizer=bias_regularizer,
                    name='layer_%02i' % l))

            self.layer_z_mean = tf.layers.Dense(
                units=self.dim_latent,
                activation=None,
                use_bias=use_bias,
                kernel_initializer=kernel_initializer,
                bias_initializer=bias_initializer,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                name='layer_z_mean')
            self.layer_z_vars = tf.layers.Dense(
                units=self.dim_latent * self.dim_latent,
                activation=None,
                use_bias=use_bias,
                kernel_initializer=kernel_initializer,
                bias_initializer=bias_initializer,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                name='layer_z_vars')

            # compute layer outputs from inference network input
            layer_input = self.input_ph
            for l in range(num_layers):
                layer_input = self.layers[l].apply(layer_input)
            self.hidden_act = layer_input

            # get data-dependent mean
            self.m_psi = self.layer_z_mean.apply(self.hidden_act)

            # get sqrt of inverse of data-dependent covariances
            r_psi_sqrt = self.layer_z_vars.apply(self.hidden_act)
            self.r_psi_sqrt = tf.reshape(
                r_psi_sqrt,
                [-1, self.num_time_pts, self.dim_latent, self.dim_latent])

        with tf.variable_scope('precision_matrix'):
            # get inverse of data-dependent covariances
            self.c_psi_inv = tf.matmul(
                self.r_psi_sqrt,
                tf.transpose(self.r_psi_sqrt, perm=[0, 1, 3, 2]),
                name='precision_diag_data_dep')

            self.AQ0invA_Qinv = tf.matmul(
                tf.matmul(self.A, self.Q0inv), self.A, transpose_b=True) \
                + self.Qinv
            self.AQinvA_Qinv = tf.matmul(
                tf.matmul(self.A, self.Qinv), self.A, transpose_b=True) \
                + self.Qinv
            self.AQ0inv = tf.matmul(-self.A, self.Q0inv)
            self.AQinv = tf.matmul(-self.A, self.Qinv)

            # put together components of precision matrix Sinv in tensor of
            # shape [num_samples, num_time_pts, dim_latent, dim_latent]
            Sinv_diag = tf.tile(
                tf.expand_dims(self.AQinvA_Qinv, 0),
                [self.num_time_pts - 2, 1, 1])
            Sinv_diag = tf.concat(
                [tf.expand_dims(self.Q0inv, 0),
                 tf.expand_dims(self.AQ0invA_Qinv, 0),
                 Sinv_diag], axis=0, name='precision_diag_static')
            self.Sinv_diag = tf.add(Sinv_diag, self.c_psi_inv,
                                    name='precision_diag')

            Sinv_ldiag = tf.tile(
                tf.expand_dims(self.AQinv, 0),
                [self.num_time_pts - 2, 1, 1], name='precision_lower_diag')
            Sinv_ldiag0 = tf.concat(
                [tf.expand_dims(self.AQ0inv, 0), Sinv_ldiag], axis=0)

            # we now have Sinv (represented as diagonal and off-diagonal
            # blocks); to sample from the posterior we need the square root
            # of the inverse of Sinv; fortunately this is fast given the
            # tridiagonal block structure of Sinv. First we'll compute the
            # Cholesky decomposition of Sinv, then calculate the inverse using
            # that decomposition

            # get cholesky decomposition for each element in batch
            def scan_chol(_, inputs):
                """inputs refer to diagonal blocks, outputs the L/U matrices"""
                chol_decomp_Sinv = blk_tridiag_chol(inputs, Sinv_ldiag0)
                return chol_decomp_Sinv

            # self.chol_decomp_Sinv = tf.scan(
            #     fn=scan_chol, elems=[self.Sinv_diag, Sinv_ldiag1])
            self.chol_decomp_Sinv = tf.scan(
                fn=scan_chol, elems=self.Sinv_diag,
                initializer=[Sinv_diag, Sinv_ldiag0],  # throwaway to get scan
                name='precision_chol_decomp')          # to behave

        # posterior mean
        with tf.variable_scope('posterior_mean'):
            ia = tf.reduce_sum(
                tf.multiply(self.c_psi_inv,
                            tf.expand_dims(self.m_psi, axis=2)),
                axis=3)
            # ia now S x T x dim_latent

            # get sample for each element in batch
            def scan_chol_inv(_, inputs):
                """inputs refer to L/U matrices, outputs to means"""
                [chol_decomp_Sinv_0, chol_decomp_Sinv_1, ia] = inputs
                # mult by R
                ib = blk_chol_inv(
                    chol_decomp_Sinv_0, chol_decomp_Sinv_1, ia,
                    lower=True, transpose=False)
                post_z_means = blk_chol_inv(
                    chol_decomp_Sinv_0, chol_decomp_Sinv_1, ib,
                    lower=False, transpose=True)

                return post_z_means

            self.post_z_means = tf.scan(
                fn=scan_chol_inv,
                elems=[self.chol_decomp_Sinv[0], self.chol_decomp_Sinv[1], ia],
                initializer=ia[0])  # throwaway to get scan to behave

        # sample from posterior
        with tf.variable_scope('posterior_samples'):

            def scan_chol_half_inv(_, inputs):
                """inputs refer to L/U matrices, outputs to means"""
                [chol_decomp_Sinv_0, chol_decomp_Sinv_1, samples] = inputs
                rands = blk_chol_inv(
                    chol_decomp_Sinv_0, chol_decomp_Sinv_1, samples,
                    lower=False, transpose=True)

                return rands

            rands = tf.scan(fn=scan_chol_half_inv,
                            elems=[self.chol_decomp_Sinv[0],
                                   self.chol_decomp_Sinv[1],
                                   self.samples_z],
                            initializer=ia[0])  # throwaway for scan to behave

            self.post_z_samples = self.post_z_means + rands


class MeanFieldGaussian(InferenceNetwork):
    """
    Approximate posterior is modeled as a fully factorized Gaussian across time
    and latent space, so that for
    x = [x_1, ..., x_T]
    and
    x_i = [x_1^i, ..., x_T^i]

    x ~ \prod_{t=1}^T \prod_{i=1}^dim_latent N( mu_t^i(y_t), sigma_t^i(y_t) )

    Each covariance sigma_t is a diagonal [dim_latent x dim_latent] covariance
    matrix.
    """

    def __init__(
            self, dim_input=None, dim_latent=None, rng=0, dtype=tf.float32,
            num_time_pts=None):

        super(MeanFieldGaussian, self).__init__(
            dim_input=dim_input, dim_latent=dim_latent, rng=rng,
            dtype=dtype)

        self.num_time_pts = num_time_pts

    def build_graph(self):
        """Build tensorflow computation graph for inference network"""

        # should eventually become user options
        tr_norm_initializer = tf.initializers.truncated_normal(
            mean=0.0, stddev=0.1, dtype=self.dtype)
        zeros_initializer = tf.initializers.zeros(dtype=self.dtype)
        activation = tf.nn.tanh
        use_bias = True
        kernel_initializer = tr_norm_initializer
        bias_initializer = zeros_initializer
        kernel_regularizer = None
        bias_regularizer = None
        num_layers = 2

        # construct data pipeline
        with tf.variable_scope('inference_input'):
            self.input_ph = [None, None]
            self.input_ph[0] = tf.placeholder(
                dtype=self.dtype,
                shape=[None, self.num_time_pts, self.dim_input],
                name='obs_in_ph')
            self.samples_z = tf.random_normal(
                shape=[tf.shape(self.input_ph)[0],
                       self.num_time_pts,
                       self.dim_latent],
                mean=0.0, stddev=1.0, dtype=self.dtype, name='samples_z')

        with tf.variable_scope('inference_mlp'):
            # store layers in a list
            self.layers = []
            for l in range(num_layers):
                self.layers.append(tf.layers.Dense(
                    units=30,
                    activation=activation,
                    use_bias=use_bias,
                    kernel_initializer=kernel_initializer,
                    bias_initializer=bias_initializer,
                    kernel_regularizer=kernel_regularizer,
                    bias_regularizer=bias_regularizer,
                    name='layer_%02i' % l))

            self.layer_z_mean = tf.layers.Dense(
                units=self.dim_latent,
                activation=None,
                use_bias=use_bias,
                kernel_initializer=kernel_initializer,
                bias_initializer=bias_initializer,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                name='layer_z_mean')
            self.layer_z_log_vars = tf.layers.Dense(
                units=self.dim_latent,
                activation=None,
                use_bias=use_bias,
                kernel_initializer=kernel_initializer,
                bias_initializer=bias_initializer,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                name='layer_z_log_vars')

            # compute layer outputs from inference network input
            layer_input = self.input_ph
            for l in range(num_layers):
                layer_input = self.layers[l].apply(layer_input)
            self.hidden_act = layer_input

            # get data-dependent mean
            self.post_z_means = self.layer_z_mean.apply(self.hidden_act)

            # get sqrt of inverse of data-dependent covariances
            self.post_z_log_vars = self.layer_z_log_vars.apply(self.hidden_act)

        # sample from posterior
        with tf.variable_scope('posterior_samples'):

            rands = tf.multiply(
                tf.sqrt(tf.exp(self.post_z_log_vars)), self.samples_z)
            self.post_z_samples = self.post_z_means + rands

    def evaluate_entropy(self):
        """Entropy of approximate posterior"""

        ln_det = -2.0 * tf.reduce_sum(
            tf.reduce_mean(self.post_z_log_vars, axis=0))

        entropy = ln_det / 2.0 + self.dim_latent * self.num_time_pts / 2.0 * (
                    1.0 + np.log(2.0 * np.pi))

        return entropy

    def generate_samples(self, sess, num_samples):
        """
        Draw samples from approximate posterior

        Args:
            sess (tf.Session object)
            num_samples (int)

        Returns:
            z (num_samples x num_time_pts x dim_latent numpy array)

        """

        z = sess.run(
            self.post_z_samples, feed_dict={self.num_samples_ph, num_samples})

        return z

    def get_posterior_means(self, sess, observations):
        """Get posterior means conditioned on observations"""

        feed_dict = {self.input_ph: observations}

        return sess.run(self.post_z_means, feed_dict=feed_dict)


# class MeanFieldGaussianTemporal(InferenceNetwork):
#     """
#     Approximate posterior is modeled as a fully factorized Gaussian in time, so
#     that for x = [x_1, ..., x_T]
#
#     x ~ \prod_{t=1}^T N( mu_t(y_t), sigma_t(y_t) )
#
#     Each covariance sigma_t is a full [dim_latent x dim_latent] covariance
#     matrix.
#     """
#
#     def __init__(self, dim_input=None, dim_latent=None, rng=0,
#                  dtype=tf.float32, num_time_pts=None):
#
#         super(MeanFieldGaussianTemporal, self).__init__(
#             dim_input=dim_input, dim_latent=dim_latent, rng=rng,
#             dtype=dtype)
#
#         self.num_time_pts = num_time_pts
#
#     def build_graph(self):
#         """Build tensorflow computation graph for inference network"""
#
#         # should eventually become user options
#         tr_norm_initializer = tf.initializers.truncated_normal(
#             mean=0.0, stddev=0.1, dtype=self.dtype)
#         zeros_initializer = tf.initializers.zeros(dtype=self.dtype)
#         activation = tf.nn.tanh
#         use_bias = True
#         kernel_initializer = tr_norm_initializer
#         bias_initializer = zeros_initializer
#         kernel_regularizer = None
#         bias_regularizer = None
#         num_layers = 2
#
#         # construct data pipeline
#         with tf.variable_scope('inference_input'):
#             self.input_ph = [None, None]
#             self.input_ph[0] = tf.placeholder(
#                 dtype=self.dtype,
#                 shape=[None, self.num_time_pts, self.dim_input],
#                 name='obs_in_ph')
#             self.input_ph[1] = tf.placeholder(
#                 dtype=self.dtype,
#                 shape=[None, self.num_time_pts, self.dim_latent],
#                 name='samples_z_ph')
#
#         with tf.variable_scope('inference_mlp'):
#             # store layers in a list
#             self.layers = []
#             for l in range(num_layers):
#                 self.layers.append(tf.layers.Dense(
#                     units=30,
#                     activation=activation,
#                     use_bias=use_bias,
#                     kernel_initializer=kernel_initializer,
#                     bias_initializer=bias_initializer,
#                     kernel_regularizer=kernel_regularizer,
#                     bias_regularizer=bias_regularizer,
#                     name='layer_%02i' % l))
#
#             self.layer_z_mean = tf.layers.Dense(
#                 units=self.dim_latent * self.num_time_pts,
#                 activation=None,
#                 use_bias=use_bias,
#                 kernel_initializer=kernel_initializer,
#                 bias_initializer=bias_initializer,
#                 kernel_regularizer=kernel_regularizer,
#                 bias_regularizer=bias_regularizer,
#                 name='layer_z_mean')
#             self.layer_z_log_vars = tf.layers.Dense(
#                 units=self.dim_latent * self.num_time_pts,
#                 activation=None,
#                 use_bias=use_bias,
#                 kernel_initializer=kernel_initializer,
#                 bias_initializer=bias_initializer,
#                 kernel_regularizer=kernel_regularizer,
#                 bias_regularizer=bias_regularizer,
#                 name='layer_z_vars')
#
#             # compute layer outputs from inference network input
#             layer_input = self.input_ph[0]
#             for l in range(num_layers):
#                 layer_input = self.layers[l].apply(layer_input)
#             self.hidden_act = layer_input
#
#             # get data-dependent mean
#             self.m_psi = self.layer_z_mean.apply(self.hidden_act)
#
#             # get sqrt of inverse of data-dependent covariances
#             r_psi_sqrt = self.layer_z_vars.apply(self.hidden_act)
#             self.r_psi_sqrt = tf.reshape(
#                 r_psi_sqrt,
#                 [-1, self.num_time_pts, self.dim_latent, self.dim_latent])
#
#         with tf.variable_scope('precision_matrix'):
#             # get inverse of data-dependent covariances
#             self.c_psi_inv = tf.matmul(
#                 self.r_psi_sqrt,
#                 tf.transpose(self.r_psi_sqrt, perm=[0, 1, 3, 2]),
#                 name='precision_diag_data_dep')
#
#             self.AQ0invA_Qinv = tf.matmul(
#                 tf.matmul(self.A, self.Q0inv), self.A, transpose_b=True) \
#                 + self.Qinv
#             self.AQinvA_Qinv = tf.matmul(
#                 tf.matmul(self.A, self.Qinv), self.A, transpose_b=True) \
#                 + self.Qinv
#             self.AQ0inv = tf.matmul(-self.A, self.Q0inv)
#             self.AQinv = tf.matmul(-self.A, self.Qinv)
#
#             # put together components of precision matrix Sinv in tensor of
#             # shape [num_samples, num_time_pts, dim_latent, dim_latent]
#             Sinv_diag = tf.tile(
#                 tf.expand_dims(self.AQinvA_Qinv, 0),
#                 [self.num_time_pts - 2, 1, 1])
#             Sinv_diag = tf.concat(
#                 [tf.expand_dims(self.Q0inv, 0),
#                  tf.expand_dims(self.AQ0invA_Qinv, 0),
#                  Sinv_diag], axis=0, name='precision_diag_static')
#             self.Sinv_diag = tf.add(Sinv_diag, self.c_psi_inv,
#                                     name='precision_diag')
#
#             Sinv_ldiag = tf.tile(
#                 tf.expand_dims(self.AQinv, 0),
#                 [self.num_time_pts - 2, 1, 1], name='precision_lower_diag')
#             Sinv_ldiag0 = tf.concat(
#                 [tf.expand_dims(self.AQ0inv, 0), Sinv_ldiag], axis=0)
#
#             # we now have Sinv (represented as diagonal and off-diagonal
#             # blocks); to sample from the posterior we need the square root
#             # of the inverse of Sinv; fortunately this is fast given the
#             # tridiagonal block structure of Sinv. First we'll compute the
#             # Cholesky decomposition of Sinv, then calculate the inverse using
#             # that decomposition
#
#             # get cholesky decomposition for each element in batch
#             def scan_chol(_, inputs):
#                 """inputs refer to diagonal blocks, outputs the L/U matrices"""
#                 chol_decomp_Sinv = blk_tridiag_chol(inputs, Sinv_ldiag0)
#                 return chol_decomp_Sinv
#
#             # self.chol_decomp_Sinv = tf.scan(
#             #     fn=scan_chol, elems=[self.Sinv_diag, Sinv_ldiag1])
#             self.chol_decomp_Sinv = tf.scan(
#                 fn=scan_chol, elems=self.Sinv_diag,
#                 initializer=[Sinv_diag, Sinv_ldiag0],  # throwaway to get scan
#                 name='precision_chol_decomp')          # to behave
#
#         # posterior mean
#         with tf.variable_scope('posterior_mean'):
#             ia = tf.reduce_sum(
#                 tf.multiply(self.c_psi_inv,
#                             tf.expand_dims(self.m_psi, axis=2)),
#                 axis=3)
#             # ia now S x T x dim_latent
#
#             # get sample for each element in batch
#             def scan_chol_inv(_, inputs):
#                 """inputs refer to L/U matrices, outputs to means"""
#                 [chol_decomp_Sinv_0, chol_decomp_Sinv_1, ia] = inputs
#                 # mult by R
#                 ib = blk_chol_inv(
#                     chol_decomp_Sinv_0, chol_decomp_Sinv_1, ia,
#                     lower=True, transpose=False)
#                 post_z_means = blk_chol_inv(
#                     chol_decomp_Sinv_0, chol_decomp_Sinv_1, ib,
#                     lower=False, transpose=True)
#
#                 return post_z_means
#
#             self.post_z_means = tf.scan(
#                 fn=scan_chol_inv,
#                 elems=[self.chol_decomp_Sinv[0], self.chol_decomp_Sinv[1], ia],
#                 initializer=ia[0])  # throwaway to get scan to behave
#
#         # sample from posterior
#         with tf.variable_scope('posterior_samples'):
#
#             def scan_chol_half_inv(_, inputs):
#                 """inputs refer to L/U matrices, outputs to means"""
#                 [chol_decomp_Sinv_0, chol_decomp_Sinv_1, samples] = inputs
#                 rands = blk_chol_inv(
#                     chol_decomp_Sinv_0, chol_decomp_Sinv_1, samples,
#                     lower=False, transpose=True)
#
#                 return rands
#
#             rands = tf.scan(fn=scan_chol_half_inv,
#                             elems=[self.chol_decomp_Sinv[0],
#                                    self.chol_decomp_Sinv[1],
#                                    self.input_ph[1]],
#                             initializer=ia[0])  # throwaway for scan to behave
#
#             self.post_z_samples = self.post_z_means + rands
#
#     def evaluate_entropy(self):
#         """Entropy of approximate posterior"""
#
#         # determinant of the covariance is the square of the determinant of the
#         # cholesky factor; determinant of the cholesky factor is the product of
#         # the diagonal elements of the block-diagonal
#         ln_det = -2.0 * tf.reduce_sum(
#             tf.reduce_mean(
#                 tf.log(tf.matrix_diag_part(self.chol_decomp_Sinv[0])), axis=0))
#
#         entropy = ln_det / 2.0 + self.dim_latent * self.num_time_pts / 2.0 * (
#                     1.0 + np.log(2.0 * np.pi))
#
#         return entropy
#
#     def generate_samples(self, sess, num_samples):
#         """
#         Draw samples from approximate posterior
#
#         Args:
#             sess (tf.Session object)
#             num_samples (int)
#
#         Returns:
#             z (num_samples x num_time_pts x dim_latent numpy array)
#
#         """
#
#         z = sess.run(
#             self.post_z_samples,
#             feed_dict={self.input_ph[1], self.random_samples(num_samples)})
#
#         return z
#
#     def get_params(self, sess):
#         """Get parameters of generative model"""
#
#         # A, z0_mean, Q, Q0 = sess.run([self.A, self.z0_mean, self.Q, self.Q0])
#         #
#         # param_dict = {'A': A, 'z0_mean': z0_mean, 'Q': Q, 'Q0': Q0}
#
#         A, z0_mean, Q_sqrt, Q, Q0_sqrt, Q0 = sess.run(
#             [self.A, self.z0_mean, self.Q_sqrt, self.Q, self.Q0_sqrt, self.Q0])
#
#         param_dict = {'A': A, 'z0_mean': z0_mean, 'Q': Q, 'Q0': Q0,
#                       'Q_sqrt': Q_sqrt, 'Q0_sqrt': Q0_sqrt}
#
#         return param_dict
#
#     def get_posterior_means(self, sess, observations):
#         """Get posterior means conditioned on observations"""
#
#         feed_dict = {self.input_ph[0]: observations}
#
#         return sess.run(self.post_z_means, feed_dict=feed_dict)
#
#     def random_samples(self, num_samples=1):
#         return np.random.normal(
#             loc=0.0, scale=1.0,
#             size=(num_samples, self.num_time_pts, self.dim_latent)). \
#             astype(self.dtype.as_numpy_dtype())