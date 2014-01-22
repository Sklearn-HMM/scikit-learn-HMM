"""
Ridge regression
"""

# Author: Mathieu Blondel <mathieu@mblondel.org>
#         Reuben Fletcher-Costin <reuben.fletchercostin@gmail.com>
#         Fabian Pedregosa <fabian@fseoane.net>
# License: BSD 3 clause


from abc import ABCMeta, abstractmethod
import warnings

import numpy as np
from scipy import linalg
from scipy import sparse
from scipy.sparse import linalg as sp_linalg

from .base import LinearClassifierMixin, LinearModel
from ..base import RegressorMixin
from ..utils.extmath import safe_sparse_dot
from ..utils import safe_asarray
from ..utils import compute_class_weight
from ..utils import column_or_1d
from ..preprocessing import LabelBinarizer
from ..grid_search import GridSearchCV
from ..externals import six
from ..metrics.scorer import _deprecate_loss_and_score_funcs


def _solve_sparse_cg(X, y, alpha, max_iter=None, tol=1e-3):
    n_samples, n_features = X.shape
    X1 = sp_linalg.aslinearoperator(X)
    coefs = np.empty((y.shape[1], n_features))

    if n_features > n_samples:
        def create_mv(curr_alpha):
            def _mv(x):
                return X1.matvec(X1.rmatvec(x)) + curr_alpha * x
            return _mv
    else:
        def create_mv(curr_alpha):
            def _mv(x):
                return X1.rmatvec(X1.matvec(x)) + curr_alpha * x
            return _mv

    for i in range(y.shape[1]):
        y_column = y[:, i]

        mv = create_mv(alpha[i])
        if n_features > n_samples:
            # kernel ridge
            # w = X.T * inv(X X^t + alpha*Id) y
            C = sp_linalg.LinearOperator(
                (n_samples, n_samples), matvec=mv, dtype=X.dtype)
            coef, info = sp_linalg.cg(C, y_column, tol=tol)
            coefs[i] = X1.rmatvec(coef)
        else:
            # linear ridge
            # w = inv(X^t X + alpha*Id) * X.T y
            y_column = X1.rmatvec(y_column)
            C = sp_linalg.LinearOperator(
                (n_features, n_features), matvec=mv, dtype=X.dtype)
            coefs[i], info = sp_linalg.cg(C, y_column, maxiter=max_iter,
                                          tol=tol)
        if info != 0:
            raise ValueError("Failed with error code %d" % info)

    return coefs


def _solve_lsqr(X, y, alpha, max_iter=None, tol=1e-3):
    n_samples, n_features = X.shape
    coefs = np.empty((y.shape[1], n_features))

    # According to the lsqr documentation, alpha = damp^2.
    sqrt_alpha = np.sqrt(alpha)

    for i in range(y.shape[1]):
        y_column = y[:, i]
        coefs[i] = sp_linalg.lsqr(X, y_column, damp=sqrt_alpha[i],
                                  atol=tol, btol=tol, iter_lim=max_iter)[0]

    return coefs


def _solve_dense_cholesky(X, y, alpha):
    # w = inv(X^t X + alpha*Id) * X.T y
    n_samples, n_features = X.shape
    n_targets = y.shape[1]

    A = safe_sparse_dot(X.T, X, dense_output=True)
    Xy = safe_sparse_dot(X.T, y, dense_output=True)

    one_alpha = np.array_equal(alpha, len(alpha) * [alpha[0]])

    if one_alpha:
        A.flat[::n_features + 1] += alpha[0]
        return linalg.solve(A, Xy, sym_pos=True,
                            overwrite_a=True).T
    else:
        coefs = np.empty([n_targets, n_features])
        for coef, target, current_alpha in zip(coefs, Xy.T, alpha):
            A.flat[::n_features + 1] += current_alpha
            coef[:] = linalg.solve(A, target, sym_pos=True,
                                   overwrite_a=False).ravel()
            A.flat[::n_features + 1] -= current_alpha
        return coefs


def _solve_dense_cholesky_kernel(K, y, alpha, sample_weight=None):
    # dual_coef = inv(X X^t + alpha*Id) y
    n_samples = K.shape[0]
    n_targets = y.shape[1]

    one_alpha = np.array_equal(alpha, len(alpha) * [alpha[0]])
    has_sw = isinstance(sample_weight, np.ndarray) or sample_weight != 1.0

    if has_sw:
        sw = np.sqrt(sample_weight)
        y = y * sw[:, np.newaxis]
        K *= np.outer(sw, sw)

    if one_alpha:
        # Only one penalty, we can solve multi-target problems in one time.
        K.flat[::n_samples + 1] += alpha[0]

        dual_coef = linalg.solve(K, y, sym_pos=True, overwrite_a=True)

        # K is expensive to compute and store in memory so change it back in
        # case it was user-given.
        K.flat[::n_samples + 1] -= alpha[0]

        if has_sw:
            dual_coef *= sw[:, np.newaxis]

        return dual_coef
    else:
        # One penalty per target. We need to solve each target separately.
        dual_coefs = np.empty([n_targets, n_samples])

        for dual_coef, target, current_alpha in zip(dual_coefs, y.T, alpha):
            K.flat[::n_samples + 1] += current_alpha

            dual_coef[:] = linalg.solve(K, target, sym_pos=True,
                                        overwrite_a=False).ravel()

            K.flat[::n_samples + 1] -= current_alpha

        if has_sw:
            dual_coefs *= sw[np.newaxis, :]

        return dual_coefs.T


def _solve_svd(X, y, alpha):
    U, s, Vt = linalg.svd(X, full_matrices=False)
    idx = s > 1e-15  # same default value as scipy.linalg.pinv
    s_nnz = s[idx][:, np.newaxis]
    UTy = np.dot(U.T, y)
    d = np.zeros((s.size, alpha.size))
    d[idx] = s_nnz / (s_nnz ** 2 + alpha)
    d_UT_y = d * UTy
    return np.dot(Vt.T, d_UT_y).T


def ridge_regression(X, y, alpha, sample_weight=1.0, solver='auto',
                     max_iter=None, tol=1e-3):
    """Solve the ridge equation by the method of normal equations.

    Parameters
    ----------
    X : {array-like, sparse matrix, LinearOperator},
        shape = [n_samples, n_features]
        Training data

    y : array-like, shape = [n_samples] or [n_samples, n_targets]
        Target values

    alpha : {float, array-like},
        shape = [n_targets] if array-like
        The l_2 penalty to be used. If an array is passed, penalties are
        assumed to be specific to targets

    max_iter : int, optional
        Maximum number of iterations for conjugate gradient solver.
        The default value is determined by scipy.sparse.linalg.

    sample_weight : float or numpy array of shape [n_samples]
        Individual weights for each sample

    solver : {'auto', 'svd', 'dense_cholesky', 'lsqr', 'sparse_cg'}
        Solver to use in the computational routines:

        - 'auto' chooses the solver automatically based on the type of data.

        - 'svd' uses a Singular Value Decomposition of X to compute the Ridge
          coefficients. More stable for singular matrices than
          'dense_cholesky'.

        - 'dense_cholesky' uses the standard scipy.linalg.solve function to
          obtain a closed-form solution via a Cholesky decomposition of
          dot(X.T, X)

        - 'sparse_cg' uses the conjugate gradient solver as found in
          scipy.sparse.linalg.cg. As an iterative algorithm, this solver is
          more appropriate than 'dense_cholesky' for large-scale data
          (possibility to set `tol` and `max_iter`).

        - 'lsqr' uses the dedicated regularized least-squares routine
          scipy.sparse.linalg.lsqr. It is the fatest but may not be available
          in old scipy versions. It also uses an iterative procedure.

        All three solvers support both dense and sparse data.

    tol: float
        Precision of the solution.

    Returns
    -------
    coef: array, shape = [n_features] or [n_targets, n_features]
        Weight vector(s).

    Notes
    -----
    This function won't compute the intercept.
    """

    n_samples, n_features = X.shape

    if y.ndim > 2:
        raise ValueError("Target y has the wrong shape %s" % str(y.shape))

    ravel = False
    if y.ndim == 1:
        y = y.reshape(-1, 1)
        ravel = True

    n_samples_, n_targets = y.shape

    if n_samples != n_samples_:
        raise ValueError("Number of samples in X and y does not correspond:"
                         " %d != %d" % (n_samples, n_samples_))

    has_sw = isinstance(sample_weight, np.ndarray) or sample_weight != 1.0

    if solver == 'auto':
        # cholesky if it's a dense array and cg in
        # any other case
        if hasattr(X, '__array__'):
            solver = 'dense_cholesky'
        else:
            solver = 'sparse_cg'

    elif solver == 'lsqr' and not hasattr(sp_linalg, 'lsqr'):
        warnings.warn("""lsqr not available on this machine, falling back
                      to sparse_cg.""")
        solver = 'sparse_cg'

    if has_sw and solver != "dense_cholesky":
        warnings.warn("""sample_weight and class_weight not supported in %s,
                      fall back to dense_cholesky.""" % solver)
        solver = 'dense_cholesky'

    # There should be either 1 or n_targets penalties
    alpha = safe_asarray(alpha).ravel()
    if alpha.size not in [1, n_targets]:
        raise ValueError("Number of targets and number of penalties "
                         "do not correspond: %d != %d"
                         % (alpha.size, n_targets))

    if alpha.size == 1 and n_targets > 1:
        alpha = np.repeat(alpha, n_targets)

    if solver not in ('sparse_cg', 'dense_cholesky', 'svd', 'lsqr'):
        ValueError('Solver %s not understood' % solver)

    if solver == 'sparse_cg':
        coef = _solve_sparse_cg(X, y, alpha, max_iter, tol)

    elif solver == "lsqr":
        coef = _solve_lsqr(X, y, alpha, max_iter, tol)

    elif solver == 'dense_cholesky':
        if n_features > n_samples or has_sw:
            K = safe_sparse_dot(X, X.T, dense_output=True)
            try:
                dual_coef = _solve_dense_cholesky_kernel(K, y, alpha,
                                                         sample_weight)
            except linalg.LinAlgError:
                # use SVD solver if matrix is singular
                solver = 'svd'

            coef = safe_sparse_dot(X.T, dual_coef, dense_output=True).T
        else:
            try:
                coef = _solve_dense_cholesky(X, y, alpha)
            except linalg.LinAlgError:
                # use SVD solver if matrix is singular
                solver = 'svd'

    if solver == 'svd':
        coef = _solve_svd(X, y, alpha)

    if ravel:
        # When y was passed as a 1d-array, we flatten the coefficients.
        coef = coef.ravel()

    return coef


class _BaseRidge(six.with_metaclass(ABCMeta, LinearModel)):

    @abstractmethod
    def __init__(self, alpha=1.0, fit_intercept=True, normalize=False,
                 copy_X=True, max_iter=None, tol=1e-3, solver="auto"):
        self.alpha = alpha
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.copy_X = copy_X
        self.max_iter = max_iter
        self.tol = tol
        self.solver = solver

    def fit(self, X, y, sample_weight=1.0):
        X = safe_asarray(X, dtype=np.float)
        y = np.asarray(y, dtype=np.float)

        X, y, X_mean, y_mean, X_std = self._center_data(
            X, y, self.fit_intercept, self.normalize, self.copy_X,
            sample_weight=sample_weight)

        self.coef_ = ridge_regression(X, y,
                                      alpha=self.alpha,
                                      sample_weight=sample_weight,
                                      max_iter=self.max_iter,
                                      tol=self.tol,
                                      solver=self.solver)
        self._set_intercept(X_mean, y_mean, X_std)
        return self


class Ridge(_BaseRidge, RegressorMixin):
    """Linear least squares with l2 regularization.

    This model solves a regression model where the loss function is
    the linear least squares function and regularization is given by
    the l2-norm. Also known as Ridge Regression or Tikhonov regularization.
    This estimator has built-in support for multi-variate regression
    (i.e., when y is a 2d-array of shape [n_samples, n_targets]).

    Parameters
    ----------
    alpha : {float, array-like}
        shape = [n_targets]
        Small positive values of alpha improve the conditioning of the problem
        and reduce the variance of the estimates.  Alpha corresponds to
        ``(2*C)^-1`` in other linear models such as LogisticRegression or
        LinearSVC. If an array is passed, penalties are assumed to be specific
        to the targets. Hence they must correspond in number.

    copy_X : boolean, optional, default True
        If True, X will be copied; else, it may be overwritten.

    fit_intercept : boolean
        Whether to calculate the intercept for this model. If set
        to false, no intercept will be used in calculations
        (e.g. data is expected to be already centered).

    max_iter : int, optional
        Maximum number of iterations for conjugate gradient solver.
        The default value is determined by scipy.sparse.linalg.

    normalize : boolean, optional, default False
        If True, the regressors X will be normalized before regression.

    solver : {'auto', 'svd', 'dense_cholesky', 'lsqr', 'sparse_cg'}
        Solver to use in the computational routines:

        - 'auto' chooses the solver automatically based on the type of data.

        - 'svd' uses a Singular Value Decomposition of X to compute the Ridge
          coefficients. More stable for singular matrices than
          'dense_cholesky'.

        - 'dense_cholesky' uses the standard scipy.linalg.solve function to
          obtain a closed-form solution.

        - 'sparse_cg' uses the conjugate gradient solver as found in
          scipy.sparse.linalg.cg. As an iterative algorithm, this solver is
          more appropriate than 'dense_cholesky' for large-scale data
          (possibility to set `tol` and `max_iter`).

        - 'lsqr' uses the dedicated regularized least-squares routine
          scipy.sparse.linalg.lsqr. It is the fatest but may not be available
          in old scipy versions. It also uses an iterative procedure.

        All three solvers support both dense and sparse data.

    tol : float
        Precision of the solution.

    Attributes
    ----------
    `coef_` : array, shape = [n_features] or [n_targets, n_features]
        Weight vector(s).

    See also
    --------
    RidgeClassifier, RidgeCV

    Examples
    --------
    >>> from sklearn.linear_model import Ridge
    >>> import numpy as np
    >>> n_samples, n_features = 10, 5
    >>> np.random.seed(0)
    >>> y = np.random.randn(n_samples)
    >>> X = np.random.randn(n_samples, n_features)
    >>> clf = Ridge(alpha=1.0)
    >>> clf.fit(X, y) # doctest: +NORMALIZE_WHITESPACE
    Ridge(alpha=1.0, copy_X=True, fit_intercept=True, max_iter=None,
          normalize=False, solver='auto', tol=0.001)
    """
    def __init__(self, alpha=1.0, fit_intercept=True, normalize=False,
                 copy_X=True, max_iter=None, tol=1e-3, solver="auto"):
        super(Ridge, self).__init__(alpha=alpha, fit_intercept=fit_intercept,
                                    normalize=normalize, copy_X=copy_X,
                                    max_iter=max_iter, tol=tol, solver=solver)

    def fit(self, X, y, sample_weight=1.0):
        """Fit Ridge regression model

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training data

        y : array-like, shape = [n_samples] or [n_samples, n_targets]
            Target values

        sample_weight : float or numpy array of shape [n_samples]
            Individual weights for each sample

        Returns
        -------
        self : returns an instance of self.
        """
        return super(Ridge, self).fit(X, y, sample_weight=sample_weight)


class RidgeClassifier(LinearClassifierMixin, _BaseRidge):
    """Classifier using Ridge regression.

    Parameters
    ----------
    alpha : float
        Small positive values of alpha improve the conditioning of the problem
        and reduce the variance of the estimates.  Alpha corresponds to
        ``(2*C)^-1`` in other linear models such as LogisticRegression or
        LinearSVC.

    class_weight : dict, optional
        Weights associated with classes in the form
        ``{class_label : weight}``. If not given, all classes are
        supposed to have weight one.

    copy_X : boolean, optional, default True
        If True, X will be copied; else, it may be overwritten.

    fit_intercept : boolean
        Whether to calculate the intercept for this model. If set to false, no
        intercept will be used in calculations (e.g. data is expected to be
        already centered).

    max_iter : int, optional
        Maximum number of iterations for conjugate gradient solver.
        The default value is determined by scipy.sparse.linalg.

    normalize : boolean, optional, default False
        If True, the regressors X will be normalized before regression.

    solver : {'auto', 'svd', 'dense_cholesky', 'lsqr', 'sparse_cg'}
        Solver to use in the computational
        routines. 'svd' will use a Sinvular value decomposition to obtain
        the solution, 'dense_cholesky' will use the standard
        scipy.linalg.solve function, 'sparse_cg' will use the
        conjugate gradient solver as found in
        scipy.sparse.linalg.cg while 'auto' will chose the most
        appropriate depending on the matrix X. 'lsqr' uses
        a direct regularized least-squares routine provided by scipy.

    tol : float
        Precision of the solution.

    Attributes
    ----------
    `coef_` : array, shape = [n_features] or [n_classes, n_features]
        Weight vector(s).

    See also
    --------
    Ridge, RidgeClassifierCV

    Notes
    -----
    For multi-class classification, n_class classifiers are trained in
    a one-versus-all approach. Concretely, this is implemented by taking
    advantage of the multi-variate response support in Ridge.
    """
    def __init__(self, alpha=1.0, fit_intercept=True, normalize=False,
                 copy_X=True, max_iter=None, tol=1e-3, class_weight=None,
                 solver="auto"):
        super(RidgeClassifier, self).__init__(
            alpha=alpha, fit_intercept=fit_intercept, normalize=normalize,
            copy_X=copy_X, max_iter=max_iter, tol=tol, solver=solver)
        self.class_weight = class_weight

    def fit(self, X, y):
        """Fit Ridge regression model.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples,n_features]
            Training data

        y : array-like, shape = [n_samples]
            Target values

        Returns
        -------
        self : returns an instance of self.
        """
        self._label_binarizer = LabelBinarizer(pos_label=1, neg_label=-1)
        Y = self._label_binarizer.fit_transform(y)
        if not self._label_binarizer.multilabel_:
            y = column_or_1d(y, warn=True)

        if self.class_weight:
            cw = compute_class_weight(self.class_weight,
                                      self.classes_, Y)
            # get the class weight corresponding to each sample
            sample_weight = cw[np.searchsorted(self.classes_, y)]
        else:
            sample_weight = 1.0

        super(RidgeClassifier, self).fit(X, Y, sample_weight=sample_weight)
        return self

    @property
    def classes_(self):
        return self._label_binarizer.classes_


class _RidgeGCV(LinearModel):
    """Ridge regression with built-in Generalized Cross-Validation

    It allows efficient Leave-One-Out cross-validation.

    This class is not intended to be used directly. Use RidgeCV instead.

    Notes
    -----

    We want to solve (K + alpha*Id)c = y,
    where K = X X^T is the kernel matrix.

    Let G = (K + alpha*Id)^-1.

    Dual solution: c = Gy
    Primal solution: w = X^T c

    Compute eigendecomposition K = Q V Q^T.
    Then G = Q (V + alpha*Id)^-1 Q^T,
    where (V + alpha*Id) is diagonal.
    It is thus inexpensive to inverse for many alphas.

    Let loov be the vector of prediction values for each example
    when the model was fitted with all examples but this example.

    loov = (KGY - diag(KG)Y) / diag(I-KG)

    Let looe be the vector of prediction errors for each example
    when the model was fitted with all examples but this example.

    looe = y - loov = c / diag(G)

    References
    ----------
    http://cbcl.mit.edu/projects/cbcl/publications/ps/MIT-CSAIL-TR-2007-025.pdf
    http://www.mit.edu/~9.520/spring07/Classes/rlsslides.pdf
    """

    def __init__(self, alphas=[0.1, 1.0, 10.0],
                 fit_intercept=True, normalize=False,
                 scoring=None, score_func=None,
                 loss_func=None, copy_X=True,
                 gcv_mode=None, store_cv_values=False):
        self.alphas = np.asarray(alphas)
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.scoring = scoring
        self.score_func = score_func
        self.loss_func = loss_func
        self.copy_X = copy_X
        self.gcv_mode = gcv_mode
        self.store_cv_values = store_cv_values

    def _pre_compute(self, X, y):
        # even if X is very sparse, K is usually very dense
        K = safe_sparse_dot(X, X.T, dense_output=True)
        v, Q = linalg.eigh(K)
        QT_y = np.dot(Q.T, y)
        return v, Q, QT_y

    def _decomp_diag(self, v_prime, Q):
        # compute diagonal of the matrix: dot(Q, dot(diag(v_prime), Q^T))
        return (v_prime * Q ** 2).sum(axis=-1)

    def _diag_dot(self, D, B):
        # compute dot(diag(D), B)
        if len(B.shape) > 1:
            # handle case where B is > 1-d
            D = D[(slice(None), ) + (np.newaxis, ) * (len(B.shape) - 1)]
        return D * B

    def _errors(self, alpha, y, v, Q, QT_y):
        # don't construct matrix G, instead compute action on y & diagonal
        w = 1.0 / (v + alpha)
        c = np.dot(Q, self._diag_dot(w, QT_y))
        G_diag = self._decomp_diag(w, Q)
        # handle case where y is 2-d
        if len(y.shape) != 1:
            G_diag = G_diag[:, np.newaxis]
        return (c / G_diag) ** 2, c

    def _values(self, alpha, y, v, Q, QT_y):
        # don't construct matrix G, instead compute action on y & diagonal
        w = 1.0 / (v + alpha)
        c = np.dot(Q, self._diag_dot(w, QT_y))
        G_diag = self._decomp_diag(w, Q)
        # handle case where y is 2-d
        if len(y.shape) != 1:
            G_diag = G_diag[:, np.newaxis]
        return y - (c / G_diag), c

    def _pre_compute_svd(self, X, y):
        if sparse.issparse(X):
            raise TypeError("SVD not supported for sparse matrices")
        U, s, _ = linalg.svd(X, full_matrices=0)
        v = s ** 2
        UT_y = np.dot(U.T, y)
        return v, U, UT_y

    def _errors_svd(self, alpha, y, v, U, UT_y):
        w = ((v + alpha) ** -1) - (alpha ** -1)
        c = np.dot(U, self._diag_dot(w, UT_y)) + (alpha ** -1) * y
        G_diag = self._decomp_diag(w, U) + (alpha ** -1)
        if len(y.shape) != 1:
            # handle case where y is 2-d
            G_diag = G_diag[:, np.newaxis]
        return (c / G_diag) ** 2, c

    def _values_svd(self, alpha, y, v, U, UT_y):
        w = ((v + alpha) ** -1) - (alpha ** -1)
        c = np.dot(U, self._diag_dot(w, UT_y)) + (alpha ** -1) * y
        G_diag = self._decomp_diag(w, U) + (alpha ** -1)
        if len(y.shape) != 1:
            # handle case when y is 2-d
            G_diag = G_diag[:, np.newaxis]
        return y - (c / G_diag), c

    def fit(self, X, y, sample_weight=1.0):
        """Fit Ridge regression model

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            Training data

        y : array-like, shape = [n_samples] or [n_samples, n_targets]
            Target values

        sample_weight : float or array-like of shape [n_samples]
            Sample weight

        Returns
        -------
        self : Returns self.
        """
        X = safe_asarray(X, dtype=np.float)
        y = np.asarray(y, dtype=np.float)

        n_samples, n_features = X.shape

        X, y, X_mean, y_mean, X_std = LinearModel._center_data(
            X, y, self.fit_intercept, self.normalize, self.copy_X,
            sample_weight=sample_weight)

        gcv_mode = self.gcv_mode
        with_sw = len(np.shape(sample_weight))

        if gcv_mode is None or gcv_mode == 'auto':
            if sparse.issparse(X) or n_features > n_samples or with_sw:
                gcv_mode = 'eigen'
            else:
                gcv_mode = 'svd'
        elif gcv_mode == "svd" and with_sw:
            # FIXME non-uniform sample weights not yet supported
            warnings.warn("non-uniform sample weights unsupported for svd, "
                          "forcing usage of eigen")
            gcv_mode = 'eigen'

        if gcv_mode == 'eigen':
            _pre_compute = self._pre_compute
            _errors = self._errors
            _values = self._values
        elif gcv_mode == 'svd':
            # assert n_samples >= n_features
            _pre_compute = self._pre_compute_svd
            _errors = self._errors_svd
            _values = self._values_svd
        else:
            raise ValueError('bad gcv_mode "%s"' % gcv_mode)

        v, Q, QT_y = _pre_compute(X, y)
        n_y = 1 if len(y.shape) == 1 else y.shape[1]
        cv_values = np.zeros((n_samples * n_y, len(self.alphas)))
        C = []

        scorer = _deprecate_loss_and_score_funcs(
            self.loss_func, self.score_func, self.scoring,
            score_overrides_loss=True
        )
        error = scorer is None
        #error = self.score_func is None and self.loss_func is None

        for i, alpha in enumerate(self.alphas):
            if error:
                out, c = _errors(sample_weight * alpha, y, v, Q, QT_y)
            else:
                out, c = _values(sample_weight * alpha, y, v, Q, QT_y)
            cv_values[:, i] = out.ravel()
            C.append(c)

        if error:
            best = cv_values.mean(axis=0).argmin()
        else:
            # The scorer want an object that will make the predictions but
            # they are already computed efficiently by _RidgeGCV. This
            # identity_estimator will just return them
            def identity_estimator():
                pass
            identity_estimator.decision_function = lambda y_predict: y_predict
            identity_estimator.predict = lambda y_predict: y_predict

            out = [scorer(identity_estimator, y.ravel(), cv_values[:, i])
                   for i in range(len(self.alphas))]
            best = np.argmax(out)

        self.alpha_ = self.alphas[best]
        self.dual_coef_ = C[best]
        self.coef_ = safe_sparse_dot(self.dual_coef_.T, X)

        self._set_intercept(X_mean, y_mean, X_std)

        if self.store_cv_values:
            if len(y.shape) == 1:
                cv_values_shape = n_samples, len(self.alphas)
            else:
                cv_values_shape = n_samples, n_y, len(self.alphas)
            self.cv_values_ = cv_values.reshape(cv_values_shape)

        return self


class _BaseRidgeCV(LinearModel):
    def __init__(self, alphas=np.array([0.1, 1.0, 10.0]),
                 fit_intercept=True, normalize=False, scoring=None,
                 score_func=None, loss_func=None, cv=None, gcv_mode=None,
                 store_cv_values=False):
        self.alphas = alphas
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.scoring = scoring
        self.score_func = score_func
        self.loss_func = loss_func
        self.cv = cv
        self.gcv_mode = gcv_mode
        self.store_cv_values = store_cv_values

    def fit(self, X, y, sample_weight=1.0):
        """Fit Ridge regression model

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]
            Training data

        y : array-like, shape = [n_samples] or [n_samples, n_targets]
            Target values

        sample_weight : float or array-like of shape [n_samples]
            Sample weight

        Returns
        -------
        self : Returns self.
        """
        if self.cv is None:
            estimator = _RidgeGCV(self.alphas,
                                  fit_intercept=self.fit_intercept,
                                  normalize=self.normalize,
                                  scoring=self.scoring,
                                  score_func=self.score_func,
                                  loss_func=self.loss_func,
                                  gcv_mode=self.gcv_mode,
                                  store_cv_values=self.store_cv_values)
            estimator.fit(X, y, sample_weight=sample_weight)
            self.alpha_ = estimator.alpha_
            if self.store_cv_values:
                self.cv_values_ = estimator.cv_values_
        else:
            if self.store_cv_values:
                raise ValueError("cv!=None and store_cv_values=True "
                                 " are incompatible")
            parameters = {'alpha': self.alphas}
            # FIXME: sample_weight must be split into training/validation data
            #        too!
            #fit_params = {'sample_weight' : sample_weight}
            fit_params = {}
            gs = GridSearchCV(Ridge(fit_intercept=self.fit_intercept),
                              parameters, fit_params=fit_params, cv=self.cv)
            gs.fit(X, y)
            estimator = gs.best_estimator_
            self.alpha_ = gs.best_estimator_.alpha

        self.coef_ = estimator.coef_
        self.intercept_ = estimator.intercept_

        return self


class RidgeCV(_BaseRidgeCV, RegressorMixin):
    """Ridge regression with built-in cross-validation.

    By default, it performs Generalized Cross-Validation, which is a form of
    efficient Leave-One-Out cross-validation.

    Parameters
    ----------
    alphas: numpy array of shape [n_alphas]
        Array of alpha values to try.
        Small positive values of alpha improve the conditioning of the
        problem and reduce the variance of the estimates.
        Alpha corresponds to ``(2*C)^-1`` in other linear models such as
        LogisticRegression or LinearSVC.

    fit_intercept : boolean
        Whether to calculate the intercept for this model. If set
        to false, no intercept will be used in calculations
        (e.g. data is expected to be already centered).

    normalize : boolean, optional, default False
        If True, the regressors X will be normalized before regression.

    scoring : string, callable or None, optional, default: None
        A string (see model evaluation documentation) or
        a scorer callable object / function with signature
        ``scorer(estimator, X, y)``.

    cv : cross-validation generator, optional
        If None, Generalized Cross-Validation (efficient Leave-One-Out)
        will be used.

    gcv_mode : {None, 'auto', 'svd', eigen'}, optional
        Flag indicating which strategy to use when performing
        Generalized Cross-Validation. Options are::

            'auto' : use svd if n_samples > n_features or when X is a sparse
                     matrix, otherwise use eigen
            'svd' : force computation via singular value decomposition of X
                    (does not work for sparse matrices)
            'eigen' : force computation via eigendecomposition of X^T X

        The 'auto' mode is the default and is intended to pick the cheaper
        option of the two depending upon the shape and format of the training
        data.

    store_cv_values : boolean, default=False
        Flag indicating if the cross-validation values corresponding to
        each alpha should be stored in the `cv_values_` attribute (see
        below). This flag is only compatible with `cv=None` (i.e. using
        Generalized Cross-Validation).

    Attributes
    ----------
    `cv_values_` : array, shape = [n_samples, n_alphas] or \
        shape = [n_samples, n_targets, n_alphas], optional
        Cross-validation values for each alpha (if `store_cv_values=True` and \
        `cv=None`). After `fit()` has been called, this attribute will \
        contain the mean squared errors (by default) or the values of the \
        `{loss,score}_func` function (if provided in the constructor).

    `coef_` : array, shape = [n_features] or [n_targets, n_features]
        Weight vector(s).

    `alpha_` : float
        Estimated regularization parameter.

    `intercept_` : float | array, shape = (n_targets,)
        Independent term in decision function. Set to 0.0 if
        ``fit_intercept = False``.

    See also
    --------
    Ridge: Ridge regression
    RidgeClassifier: Ridge classifier
    RidgeClassifierCV: Ridge classifier with built-in cross validation
    """
    pass


class RidgeClassifierCV(LinearClassifierMixin, _BaseRidgeCV):
    """Ridge classifier with built-in cross-validation.

    By default, it performs Generalized Cross-Validation, which is a form of
    efficient Leave-One-Out cross-validation. Currently, only the n_features >
    n_samples case is handled efficiently.

    Parameters
    ----------
    alphas: numpy array of shape [n_alphas]
        Array of alpha values to try.
        Small positive values of alpha improve the conditioning of the
        problem and reduce the variance of the estimates.
        Alpha corresponds to ``(2*C)^-1`` in other linear models such as
        LogisticRegression or LinearSVC.

    fit_intercept : boolean
        Whether to calculate the intercept for this model. If set
        to false, no intercept will be used in calculations
        (e.g. data is expected to be already centered).

    normalize : boolean, optional, default False
        If True, the regressors X will be normalized before regression.

    scoring : string, callable or None, optional, default: None
        A string (see model evaluation documentation) or
        a scorer callable object / function with signature
        ``scorer(estimator, X, y)``.

    cv : cross-validation generator, optional
        If None, Generalized Cross-Validation (efficient Leave-One-Out)
        will be used.

    class_weight : dict, optional
        Weights associated with classes in the form
        ``{class_label : weight}``. If not given, all classes are
        supposed to have weight one.

    Attributes
    ----------
    `cv_values_` : array, shape = [n_samples, n_alphas] or \
    shape = [n_samples, n_responses, n_alphas], optional
        Cross-validation values for each alpha (if `store_cv_values=True` and
    `cv=None`). After `fit()` has been called, this attribute will contain \
    the mean squared errors (by default) or the values of the \
    `{loss,score}_func` function (if provided in the constructor).

    `coef_` : array, shape = [n_features] or [n_targets, n_features]
        Weight vector(s).

    `alpha_` : float
        Estimated regularization parameter

    See also
    --------
    Ridge: Ridge regression
    RidgeClassifier: Ridge classifier
    RidgeCV: Ridge regression with built-in cross validation

    Notes
    -----
    For multi-class classification, n_class classifiers are trained in
    a one-versus-all approach. Concretely, this is implemented by taking
    advantage of the multi-variate response support in Ridge.
    """
    def __init__(self, alphas=np.array([0.1, 1.0, 10.0]), fit_intercept=True,
                 normalize=False, score_func=None, loss_func=None, cv=None,
                 class_weight=None):
        super(RidgeClassifierCV, self).__init__(
            alphas=alphas, fit_intercept=fit_intercept, normalize=normalize,
            score_func=score_func, loss_func=loss_func, cv=cv)
        self.class_weight = class_weight

    def fit(self, X, y, sample_weight=1.0, class_weight=None):
        """Fit the ridge classifier.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training vectors, where n_samples is the number of samples
            and n_features is the number of features.

        y : array-like, shape (n_samples,)
            Target values.

        sample_weight : float or numpy array of shape (n_samples,)
            Sample weight.

        class_weight : dict, optional
            Weights associated with classes in the form
            ``{class_label : weight}``. If not given, all classes are
            supposed to have weight one. This is parameter is
            deprecated.

        Returns
        -------
        self : object
            Returns self.
        """
        if class_weight is None:
            class_weight = self.class_weight
        else:
            warnings.warn("'class_weight' is now an initialization parameter."
                          " Using it in the 'fit' method is deprecated and "
                          "will be removed in 0.15.", DeprecationWarning,
                          stacklevel=2)

        self._label_binarizer = LabelBinarizer(pos_label=1, neg_label=-1)
        Y = self._label_binarizer.fit_transform(y)
        if not self._label_binarizer.multilabel_:
            y = column_or_1d(y, warn=True)
        cw = compute_class_weight(class_weight,
                                  self.classes_, Y)
        # modify the sample weights with the corresponding class weight
        sample_weight *= cw[np.searchsorted(self.classes_, y)]
        _BaseRidgeCV.fit(self, X, Y, sample_weight=sample_weight)
        return self

    @property
    def classes_(self):
        return self._label_binarizer.classes_
