# Author: Alexandre Gramfort <alexandre.gramfort@inria.fr>
#         Fabian Pedregosa <fabian.pedregosa@inria.fr>
#         Olivier Grisel <olivier.grisel@ensta.org>
#         Gael Varoquaux <gael.varoquaux@inria.fr>
#
# License: BSD 3 clause

import sys
import warnings
import itertools
import operator
from abc import ABCMeta, abstractmethod

import numpy as np
from scipy import sparse

from .base import LinearModel, _pre_fit
from ..base import RegressorMixin
from .base import center_data
from ..utils import array2d, atleast2d_or_csc
from ..cross_validation import _check_cv as check_cv
from ..externals.joblib import Parallel, delayed
from ..externals import six
from ..externals.six.moves import xrange
from ..utils.extmath import safe_sparse_dot

from . import cd_fast


###############################################################################
# Paths functions

def _alpha_grid(X, y, Xy=None, l1_ratio=1.0, fit_intercept=True,
                eps=1e-3, n_alphas=100, normalize=False, copy_X=True):
    """ Compute the grid of alpha values for elastic net parameter search

    Parameters
    ----------
    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data. Pass directly as Fortran-contiguous data to avoid
        unnecessary memory duplication

    y : ndarray, shape = (n_samples,)
        Target values

    Xy : array-like, optional
        Xy = np.dot(X.T, y) that can be precomputed.

    l1_ratio : float
        The ElasticNet mixing parameter, with ``0 <= l1_ratio <= 1``.
        For ``l1_ratio = 0`` the penalty is an L2 penalty. ``For
        l1_ratio = 1`` it is an L1 penalty.  For ``0 < l1_ratio <
        1``, the penalty is a combination of L1 and L2.

    eps : float, optional
        Length of the path. ``eps=1e-3`` means that
        ``alpha_min / alpha_max = 1e-3``

    n_alphas : int, optional
        Number of alphas along the regularization path

    fit_intercept : bool
        Fit or not an intercept

    normalize : boolean, optional, default False
        If ``True``, the regressors X will be normalized before regression.

    copy_X : boolean, optional, default True
        If ``True``, X will be copied; else, it may be overwritten.
    """
    if Xy is None:
        X = atleast2d_or_csc(X, copy=(copy_X and fit_intercept and not
                                      sparse.isspmatrix(X)))
        if not sparse.isspmatrix(X):
            # X can be touched inplace thanks to the above line
            X, y, _, _, _ = center_data(X, y, fit_intercept,
                                        normalize, copy=False)

        Xy = safe_sparse_dot(X.T, y, dense_output=True)
        n_samples = X.shape[0]
    else:
        n_samples = len(y)

    alpha_max = np.abs(Xy).max() / (n_samples * l1_ratio)
    alphas = np.logspace(np.log10(alpha_max * eps), np.log10(alpha_max),
                         num=n_alphas)[::-1]
    return alphas


def lasso_path(X, y, eps=1e-3, n_alphas=100, alphas=None,
               precompute='auto', Xy=None, fit_intercept=None,
               normalize=None, copy_X=True, coef_init=None,
               verbose=False, return_models=False,
               **params):
    """Compute Lasso path with coordinate descent

    The optimization objective for Lasso is::

        (1 / (2 * n_samples)) * ||y - Xw||^2_2 + alpha * ||w||_1

    Parameters
    ----------
    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data. Pass directly as Fortran-contiguous data to avoid
        unnecessary memory duplication

    y : ndarray, shape = (n_samples,)
        Target values

    eps : float, optional
        Length of the path. ``eps=1e-3`` means that
        ``alpha_min / alpha_max = 1e-3``

    n_alphas : int, optional
        Number of alphas along the regularization path

    alphas : ndarray, optional
        List of alphas where to compute the models.
        If ``None`` alphas are set automatically

    precompute : True | False | 'auto' | array-like
        Whether to use a precomputed Gram matrix to speed up
        calculations. If set to ``'auto'`` let us decide. The Gram
        matrix can also be passed as argument.

    Xy : array-like, optional
        Xy = np.dot(X.T, y) that can be precomputed. It is useful
        only when the Gram matrix is precomputed.

    fit_intercept : bool
        Fit or not an intercept.
        WARNING : will be deprecated in 0.16

    normalize : boolean, optional, default False
        If ``True``, the regressors X will be normalized before regression.
        WARNING : will be deprecated in 0.16

    copy_X : boolean, optional, default True
        If ``True``, X will be copied; else, it may be overwritten.

    coef_init : array, shape (n_features, ) | None
        The initial values of the coefficients.

    verbose : bool or integer
        Amount of verbosity

    return_models : boolean, optional, default True
        If ``True``, the function will return list of models. Setting it
        to ``False`` will change the function output returning the values
        of the alphas and the coefficients along the path. Returning the
        model list will be removed in version 0.16.

    params : kwargs
        keyword arguments passed to the coordinate descent solver.

    Returns
    -------
    models : a list of models along the regularization path
        (Is returned if ``return_models`` is set ``True`` (default).

    alphas : array, shape: [n_alphas + 1]
        The alphas along the path where models are computed.
        (Is returned, along with ``coefs``, when ``return_models`` is set
        to ``False``)

    coefs : shape (n_features, n_alphas + 1)
        Coefficients along the path.
        (Is returned, along with ``alphas``, when ``return_models`` is set
        to ``False``).

    dual_gaps : shape (n_alphas + 1)
        The dual gaps at the end of the optimization for each alpha.
        (Is returned, along with ``alphas``, when ``return_models`` is set
        to ``False``).

    Notes
    -----
    See examples/linear_model/plot_lasso_coordinate_descent_path.py
    for an example.

    To avoid unnecessary memory duplication the X argument of the fit method
    should be directly passed as a Fortran-contiguous numpy array.

    Note that in certain cases, the Lars solver may be significantly
    faster to implement this functionality. In particular, linear
    interpolation can be used to retrieve model coefficients between the
    values output by lars_path

    Deprecation Notice: Setting ``return_models`` to ``False`` will make
    the Lasso Path return an output in the style used by :func:`lars_path`.
    This will be become the norm as of version 0.16. Leaving ``return_models``
    set to `True` will let the function return a list of models as before.

    Examples
    ---------

    Comparing lasso_path and lars_path with interpolation:

    >>> X = np.array([[1, 2, 3.1], [2.3, 5.4, 4.3]]).T
    >>> y = np.array([1, 2, 3.1])
    >>> # Use lasso_path to compute a coefficient path
    >>> _, coef_path, _ = lasso_path(X, y, alphas=[5., 1., .5],
    ...                              fit_intercept=False)
    >>> print(coef_path)
    [[ 0.          0.          0.46874778]
     [ 0.2159048   0.4425765   0.23689075]]

    >>> # Now use lars_path and 1D linear interpolation to compute the
    >>> # same path
    >>> from sklearn.linear_model import lars_path
    >>> alphas, active, coef_path_lars = lars_path(X, y, method='lasso')
    >>> from scipy import interpolate
    >>> coef_path_continuous = interpolate.interp1d(alphas[::-1],
    ...                                             coef_path_lars[:, ::-1])
    >>> print(coef_path_continuous([5., 1., .5]))
    [[ 0.          0.          0.46915237]
     [ 0.2159048   0.4425765   0.23668876]]


    See also
    --------
    lars_path
    Lasso
    LassoLars
    LassoCV
    LassoLarsCV
    sklearn.decomposition.sparse_encode
    """
    return enet_path(X, y, l1_ratio=1., eps=eps, n_alphas=n_alphas,
                     alphas=alphas, precompute=precompute, Xy=Xy,
                     fit_intercept=fit_intercept, normalize=normalize,
                     copy_X=copy_X, coef_init=coef_init, verbose=verbose,
                     return_models=return_models, **params)


def enet_path(X, y, l1_ratio=0.5, eps=1e-3, n_alphas=100, alphas=None,
              precompute='auto', Xy=None, fit_intercept=True,
              normalize=False, copy_X=True, coef_init=None,
              verbose=False, return_models=False,
              **params):
    """Compute Elastic-Net path with coordinate descent

    The Elastic Net optimization function is::

        1 / (2 * n_samples) * ||y - Xw||^2_2 +
        + alpha * l1_ratio * ||w||_1
        + 0.5 * alpha * (1 - l1_ratio) * ||w||^2_2

    Parameters
    ----------
    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data. Pass directly as Fortran-contiguous data to avoid
        unnecessary memory duplication

    y : ndarray, shape = (n_samples,)
        Target values

    l1_ratio : float, optional
        float between 0 and 1 passed to ElasticNet (scaling between
        l1 and l2 penalties). ``l1_ratio=1`` corresponds to the Lasso

    eps : float
        Length of the path. ``eps=1e-3`` means that
        ``alpha_min / alpha_max = 1e-3``

    n_alphas : int, optional
        Number of alphas along the regularization path

    alphas : ndarray, optional
        List of alphas where to compute the models.
        If None alphas are set automatically

    precompute : True | False | 'auto' | array-like
        Whether to use a precomputed Gram matrix to speed up
        calculations. If set to ``'auto'`` let us decide. The Gram
        matrix can also be passed as argument.

    Xy : array-like, optional
        Xy = np.dot(X.T, y) that can be precomputed. It is useful
        only when the Gram matrix is precomputed.

    fit_intercept : bool
        Fit or not an intercept.
        WARNING : will be deprecated in 0.16

    normalize : boolean, optional, default False
        If ``True``, the regressors X will be normalized before regression.
        WARNING : will be deprecated in 0.16

    copy_X : boolean, optional, default True
        If ``True``, X will be copied; else, it may be overwritten.

    coef_init : array, shape (n_features, ) | None
        The initial values of the coefficients.

    verbose : bool or integer
        Amount of verbosity

    return_models : boolean, optional, default False
        If ``True``, the function will return list of models. Setting it
        to ``False`` will change the function output returning the values
        of the alphas and the coefficients along the path. Returning the
        model list will be removed in version 0.16.

    params : kwargs
        keyword arguments passed to the coordinate descent solver.

    Returns
    -------
    models : a list of models along the regularization path
        (Is returned if ``return_models`` is set ``True`` (default).

    alphas : array, shape: [n_alphas + 1]
        The alphas along the path where models are computed.
        (Is returned, along with ``coefs``, when ``return_models`` is set
        to ``False``)

    coefs : shape (n_features, n_alphas + 1)
        Coefficients along the path.
        (Is returned, along with ``alphas``, when ``return_models`` is set
        to ``False``).

    dual_gaps : shape (n_alphas + 1)
        The dual gaps at the end of the optimization for each alpha.
        (Is returned, along with ``alphas``, when ``return_models`` is set
        to ``False``).

    Notes
    -----
    See examples/plot_lasso_coordinate_descent_path.py for an example.

    Deprecation Notice: Setting ``return_models`` to ``False`` will make
    the Lasso Path return an output in the style used by :func:`lars_path`.
    This will be become the norm as of version 0.15. Leaving ``return_models``
    set to `True` will let the function return a list of models as before.

    See also
    --------
    ElasticNet
    ElasticNetCV
    """
    if return_models:
        warnings.warn("Use enet_path(return_models=False), as it returns the"
                      " coefficients and alphas instead of just a list of"
                      " models as previously `lasso_path`/`enet_path` did."
                      " `return_models` will eventually be removed in 0.16,"
                      " after which, returning alphas and coefs"
                      " will become the norm.",
                      DeprecationWarning, stacklevel=2)

    if normalize is True:
        warnings.warn("normalize param will be removed in 0.16."
                      " Intercept fitting and feature normalization will be"
                      " done in estimators.",
                      DeprecationWarning, stacklevel=2)
    else:
        normalize = False

    if fit_intercept is True or fit_intercept is None:
        warnings.warn("fit_intercept param will be removed in 0.16."
                      " Intercept fitting and feature normalization will be"
                      " done in estimators.",
                      DeprecationWarning, stacklevel=2)

    if fit_intercept is None:
        fit_intercept = True

    X = atleast2d_or_csc(X, dtype=np.float64, order='F',
                         copy=copy_X and fit_intercept)

    n_samples, n_features = X.shape

    if sparse.isspmatrix(X):
        if 'X_mean' in params:
            # As sparse matrices are not actually centered we need this
            # to be passed to the CD solver.
            X_sparse_scaling = params['X_mean'] / params['X_std']
        else:
            X_sparse_scaling = np.ones(n_features)

    X, y, X_mean, y_mean, X_std, precompute, Xy = \
        _pre_fit(X, y, Xy, precompute, normalize, fit_intercept, copy=False)

    n_samples = X.shape[0]
    if alphas is None:
        # No need to normalize of fit_intercept: it has been done
        # above
        alphas = _alpha_grid(X, y, Xy=Xy, l1_ratio=l1_ratio,
                             fit_intercept=False, eps=eps, n_alphas=n_alphas,
                             normalize=False, copy_X=False)
    else:
        alphas = np.sort(alphas)[::-1]  # make sure alphas are properly ordered

    n_alphas = len(alphas)

    if coef_init is None:
        coef_ = np.zeros(n_features, dtype=np.float64)
    else:
        coef_ = coef_init

    models = []
    coefs = np.empty((n_features, n_alphas), dtype=np.float64)
    dual_gaps = np.empty(n_alphas)

    tol = params.get('tol', 1e-4)
    positive = params.get('positive', False)
    max_iter = params.get('max_iter', 1000)

    for i, alpha in enumerate(alphas):
        l1_reg = alpha * l1_ratio * n_samples
        l2_reg = alpha * (1.0 - l1_ratio) * n_samples

        if sparse.isspmatrix(X):
            coef_, dual_gap_, eps_ = cd_fast.sparse_enet_coordinate_descent(
                coef_, l1_reg, l2_reg, X.data, X.indices,
                X.indptr, y, X_sparse_scaling,
                max_iter, tol, positive)
        else:
            coef_, dual_gap_, eps_ = cd_fast.enet_coordinate_descent(
                coef_, l1_reg, l2_reg, X, y, max_iter, tol, positive)

        if dual_gap_ > eps_:
            warnings.warn('Objective did not converge.' +
                          ' You might want' +
                          ' to increase the number of iterations')

        coefs[:, i] = coef_
        dual_gaps[i] = dual_gap_

        if return_models:
            model = ElasticNet(
                alpha=alpha, l1_ratio=l1_ratio,
                fit_intercept=fit_intercept if sparse.isspmatrix(X) else False,
                precompute=precompute)
            model.coef_ = coefs[:, i]
            model.dual_gap_ = dual_gaps[-1]
            if fit_intercept and not sparse.isspmatrix(X):
                model.fit_intercept = True
                model._set_intercept(X_mean, y_mean, X_std)
            models.append(model)

        if verbose:
            if verbose > 2:
                print(model)
            elif verbose > 1:
                print('Path: %03i out of %03i' % (i, n_alphas))
            else:
                sys.stderr.write('.')

    if return_models:
        return models
    else:
        return alphas, coefs, dual_gaps


###############################################################################
# ElasticNet model


class ElasticNet(LinearModel, RegressorMixin):
    """Linear Model trained with L1 and L2 prior as regularizer

    Minimizes the objective function::

            1 / (2 * n_samples) * ||y - Xw||^2_2 +
            + alpha * l1_ratio * ||w||_1
            + 0.5 * alpha * (1 - l1_ratio) * ||w||^2_2

    If you are interested in controlling the L1 and L2 penalty
    separately, keep in mind that this is equivalent to::

            a * L1 + b * L2

    where::

            alpha = a + b and l1_ratio = a / (a + b)

    The parameter l1_ratio corresponds to alpha in the glmnet R package while
    alpha corresponds to the lambda parameter in glmnet. Specifically, l1_ratio
    = 1 is the lasso penalty. Currently, l1_ratio <= 0.01 is not reliable,
    unless you supply your own sequence of alpha.

    Parameters
    ----------
    alpha : float
        Constant that multiplies the penalty terms. Defaults to 1.0
        See the notes for the exact mathematical meaning of this
        parameter.
        ``alpha = 0`` is equivalent to an ordinary least square, solved
        by the :class:`LinearRegression` object. For numerical
        reasons, using ``alpha = 0`` with the Lasso object is not advised
        and you should prefer the LinearRegression object.

    l1_ratio : float
        The ElasticNet mixing parameter, with ``0 <= l1_ratio <= 1``. For
        ``l1_ratio = 0`` the penalty is an L2 penalty. ``For l1_ratio = 1`` it
        is an L1 penalty.  For ``0 < l1_ratio < 1``, the penalty is a
        combination of L1 and L2.

    fit_intercept: bool
        Whether the intercept should be estimated or not. If ``False``, the
        data is assumed to be already centered.

    normalize : boolean, optional, default False
        If ``True``, the regressors X will be normalized before regression.

    precompute : True | False | 'auto' | array-like
        Whether to use a precomputed Gram matrix to speed up
        calculations. If set to ``'auto'`` let us decide. The Gram
        matrix can also be passed as argument. For sparse input
        this option is always ``True`` to preserve sparsity.

    max_iter: int, optional
        The maximum number of iterations

    copy_X : boolean, optional, default False
        If ``True``, X will be copied; else, it may be overwritten.

    tol: float, optional
        The tolerance for the optimization: if the updates are
        smaller than ``tol``, the optimization code checks the
        dual gap for optimality and continues until it is smaller
        than ``tol``.

    warm_start : bool, optional
        When set to ``True``, reuse the solution of the previous call to fit as
        initialization, otherwise, just erase the previous solution.

    positive: bool, optional
        When set to ``True``, forces the coefficients to be positive.

    Attributes
    ----------
    ``coef_`` : array, shape = (n_features,) | (n_targets, n_features)
        parameter vector (w in the cost function formula)

    ``sparse_coef_`` : scipy.sparse matrix, shape = (n_features, 1) | \
            (n_targets, n_features)
        ``sparse_coef_`` is a readonly property derived from ``coef_``

    ``intercept_`` : float | array, shape = (n_targets,)
        independent term in decision function.

    Notes
    -----
    To avoid unnecessary memory duplication the X argument of the fit method
    should be directly passed as a Fortran-contiguous numpy array.
    """
    path = staticmethod(enet_path)

    def __init__(self, alpha=1.0, l1_ratio=0.5, fit_intercept=True,
                 normalize=False, precompute='auto', max_iter=1000,
                 copy_X=True, tol=1e-4, warm_start=False, positive=False):
        self.alpha = alpha
        self.l1_ratio = l1_ratio
        self.coef_ = None
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.precompute = precompute
        self.max_iter = max_iter
        self.copy_X = copy_X
        self.tol = tol
        self.warm_start = warm_start
        self.positive = positive
        self.intercept_ = 0.0

    def fit(self, X, y):
        """Fit model with coordinate descent.

        Parameters
        -----------
        X : ndarray or scipy.sparse matrix, (n_samples, n_features)
            Data

        y : ndarray, shape = (n_samples,) or (n_samples, n_targets)
            Target

        Notes
        -----

        Coordinate descent is an algorithm that considers each column of
        data at a time hence it will automatically convert the X input
        as a Fortran-contiguous numpy array if necessary.

        To avoid memory re-allocation it is advised to allocate the
        initial data in memory directly using that format.
        """
        if self.alpha == 0:
            warnings.warn("With alpha=0, this algorithm does not converge "
                          "well. You are advised to use the LinearRegression "
                          "estimator", stacklevel=2)
        X = atleast2d_or_csc(X, dtype=np.float64, order='F',
                             copy=self.copy_X and self.fit_intercept)
        # From now on X can be touched inplace
        y = np.asarray(y, dtype=np.float64)

        X, y, X_mean, y_mean, X_std, precompute, Xy = \
            _pre_fit(X, y, None, self.precompute, self.normalize,
                     self.fit_intercept, copy=True)

        if y.ndim == 1:
            y = y[:, np.newaxis]
        if Xy is not None and Xy.ndim == 1:
            Xy = Xy[:, np.newaxis]

        n_samples, n_features = X.shape
        n_targets = y.shape[1]

        if not self.warm_start or self.coef_ is None:
            coef_ = np.zeros((n_targets, n_features), dtype=np.float64,
                                  order='F')
        else:
            coef_ = self.coef_
            if coef_.ndim == 1:
                coef_ = coef_[np.newaxis, :]

        dual_gaps_ = np.zeros(n_targets, dtype=np.float64)

        for k in xrange(n_targets):
            if Xy is not None:
                this_Xy = Xy[:, k]
            else:
                this_Xy = None
            _, this_coef, this_dual_gap = \
                self.path(X, y[:, k],
                          l1_ratio=self.l1_ratio, eps=None,
                          n_alphas=None, alphas=[self.alpha],
                          precompute=precompute, Xy=this_Xy,
                          fit_intercept=False, normalize=False, copy_X=True,
                          verbose=False, tol=self.tol, positive=self.positive,
                          X_mean=X_mean, X_std=X_std,
                          coef_init=coef_[k], max_iter=self.max_iter)
            coef_[k] = this_coef[:, 0]
            dual_gaps_[k] = this_dual_gap[0]

        self.coef_, self.dual_gap_ = map(np.squeeze, [coef_, dual_gaps_])
        self._set_intercept(X_mean, y_mean, X_std)

        # return self for chaining fit and predict calls
        return self

    @property
    def sparse_coef_(self):
        """ sparse representation of the fitted coef """
        return sparse.csr_matrix(self.coef_)

    def decision_function(self, X):
        """Decision function of the linear model

        Parameters
        ----------
        X : numpy array or scipy.sparse matrix of shape (n_samples, n_features)

        Returns
        -------
        T : array, shape = (n_samples,)
            The predicted decision function
        """
        if sparse.isspmatrix(X):
            return np.ravel(safe_sparse_dot(self.coef_, X.T, dense_output=True)
                            + self.intercept_)
        else:
            return super(ElasticNet, self).decision_function(X)


###############################################################################
# Lasso model

class Lasso(ElasticNet):
    """Linear Model trained with L1 prior as regularizer (aka the Lasso)

    The optimization objective for Lasso is::

        (1 / (2 * n_samples)) * ||y - Xw||^2_2 + alpha * ||w||_1

    Technically the Lasso model is optimizing the same objective function as
    the Elastic Net with ``l1_ratio=1.0`` (no L2 penalty).

    Parameters
    ----------
    alpha : float, optional
        Constant that multiplies the L1 term. Defaults to 1.0.
        ``alpha = 0`` is equivalent to an ordinary least square, solved
        by the :class:`LinearRegression` object. For numerical
        reasons, using ``alpha = 0`` is with the Lasso object is not advised
        and you should prefer the LinearRegression object.

    fit_intercept : boolean
        whether to calculate the intercept for this model. If set
        to false, no intercept will be used in calculations
        (e.g. data is expected to be already centered).

    normalize : boolean, optional, default False
        If ``True``, the regressors X will be normalized before regression.

    copy_X : boolean, optional, default True
        If ``True``, X will be copied; else, it may be overwritten.

    precompute : True | False | 'auto' | array-like
        Whether to use a precomputed Gram matrix to speed up
        calculations. If set to ``'auto'`` let us decide. The Gram
        matrix can also be passed as argument. For sparse input
        this option is always ``True`` to preserve sparsity.

    max_iter: int, optional
        The maximum number of iterations

    tol : float, optional
        The tolerance for the optimization: if the updates are
        smaller than ``tol``, the optimization code checks the
        dual gap for optimality and continues until it is smaller
        than ``tol``.

    warm_start : bool, optional
        When set to True, reuse the solution of the previous call to fit as
        initialization, otherwise, just erase the previous solution.

    positive : bool, optional
        When set to ``True``, forces the coefficients to be positive.

    Attributes
    ----------
    ``coef_`` : array, shape = (n_features,) | (n_targets, n_features)
        parameter vector (w in the cost function formula)

    ``sparse_coef_`` : scipy.sparse matrix, shape = (n_features, 1) | \
            (n_targets, n_features)
        ``sparse_coef_`` is a readonly property derived from ``coef_``

    ``intercept_`` : float | array, shape = (n_targets,)
        independent term in decision function.

    Examples
    --------
    >>> from sklearn import linear_model
    >>> clf = linear_model.Lasso(alpha=0.1)
    >>> clf.fit([[0,0], [1, 1], [2, 2]], [0, 1, 2])
    Lasso(alpha=0.1, copy_X=True, fit_intercept=True, max_iter=1000,
       normalize=False, positive=False, precompute='auto', tol=0.0001,
       warm_start=False)
    >>> print(clf.coef_)
    [ 0.85  0.  ]
    >>> print(clf.intercept_)
    0.15

    See also
    --------
    lars_path
    lasso_path
    LassoLars
    LassoCV
    LassoLarsCV
    sklearn.decomposition.sparse_encode

    Notes
    -----
    The algorithm used to fit the model is coordinate descent.

    To avoid unnecessary memory duplication the X argument of the fit method
    should be directly passed as a Fortran-contiguous numpy array.
    """
    path = staticmethod(enet_path)

    def __init__(self, alpha=1.0, fit_intercept=True, normalize=False,
                 precompute='auto', copy_X=True, max_iter=1000,
                 tol=1e-4, warm_start=False, positive=False):
        super(Lasso, self).__init__(
            alpha=alpha, l1_ratio=1.0, fit_intercept=fit_intercept,
            normalize=normalize, precompute=precompute, copy_X=copy_X,
            max_iter=max_iter, tol=tol, warm_start=warm_start,
            positive=positive)


###############################################################################
# Functions for CV with paths functions

def _path_residuals(X, y, train, test, path, path_params, l1_ratio=1,
                    X_order=None, dtype=None):
    """Returns the MSE for the models computed by 'path'

    Parameters
    ----------
    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data.

    y : array-like, shape (n_samples,) or (n_samples, n_targets)
        Target values

    train : list of indices
        The indices of the train set

    test : list of indices
        The indices of the test set

    path : callable
        function returning a list of models on the path. See
        enet_path for an example of signature

    path_params : dictionary
        Parameters passed to the path function

    l1_ratio : float, optional
        float between 0 and 1 passed to ElasticNet (scaling between
        l1 and l2 penalties). For ``l1_ratio = 0`` the penalty is an
        L2 penalty. For ``l1_ratio = 1`` it is an L1 penalty. For ``0
        < l1_ratio < 1``, the penalty is a combination of L1 and L2

    X_order : {'F', 'C', or None}, optional
        The order of the arrays expected by the path function to
        avoid memory copies

    dtype: a numpy dtype or None
        The dtype of the arrays expected by the path function to
        avoid memory copies
    """
    X_train = X[train]
    y_train = y[train]
    X_test = X[test]
    y_test = y[test]
    fit_intercept = path_params['fit_intercept']
    normalize = path_params['normalize']
    precompute = path_params['precompute']

    X_train, y_train, X_mean, y_mean, X_std, precompute, Xy = \
        _pre_fit(X_train, y_train, None, precompute, normalize, fit_intercept,
                 copy=False)

    # del path_params['precompute']
    path_params = path_params.copy()
    path_params['fit_intercept'] = False
    path_params['normalize'] = False
    path_params['Xy'] = Xy
    path_params['X_mean'] = X_mean
    path_params['X_std'] = X_std
    path_params['precompute'] = precompute
    path_params['copy_X'] = False

    if 'l1_ratio' in path_params:
        path_params['l1_ratio'] = l1_ratio

    # Do the ordering and type casting here, as if it is done in the path,
    # X is copied and a reference is kept here
    X_train = atleast2d_or_csc(X_train, dtype=dtype, order=X_order)
    alphas, coefs, _ = path(X_train, y[train], **path_params)
    del X_train

    if normalize:
        nonzeros = np.flatnonzero(X_std)
        coefs[nonzeros] /= X_std[nonzeros][:, np.newaxis]

    intercepts = y_mean - np.dot(X_mean, coefs)
    residues = safe_sparse_dot(X_test, coefs) - y_test[:, np.newaxis]
    residues += intercepts[np.newaxis, :]
    this_mses = (residues ** 2).mean(axis=0)
    return this_mses, l1_ratio


class LinearModelCV(six.with_metaclass(ABCMeta, LinearModel)):
    """Base class for iterative model fitting along a regularization path"""

    @abstractmethod
    def __init__(self, eps=1e-3, n_alphas=100, alphas=None, fit_intercept=True,
                 normalize=False, precompute='auto', max_iter=1000, tol=1e-4,
                 copy_X=True, cv=None, verbose=False):
        self.eps = eps
        self.n_alphas = n_alphas
        self.alphas = alphas
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.precompute = precompute
        self.max_iter = max_iter
        self.tol = tol
        self.copy_X = copy_X
        self.cv = cv
        self.verbose = verbose

    def fit(self, X, y):
        """Fit linear model with coordinate descent

        Fit is on grid of alphas and best alpha estimated by cross-validation.

        Parameters
        ----------

        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            Training data. Pass directly as float64, Fortran-contiguous data
            to avoid unnecessary memory duplication

        y : array-like, shape (n_samples,) or (n_samples, n_targets)
            Target values

        """
        # Dealing right with copy_X is important in the following:
        # multiple functions touch X and subsamples of X and can induce a
        # lot of duplication of memory
        copy_X = self.copy_X and self.fit_intercept

        if isinstance(X, np.ndarray) or sparse.isspmatrix(X):
            # Keep a reference to X
            reference_to_old_X = X
            # Let us not impose fortran ordering or float64 so far: it is
            # not useful for the cross-validation loop and will be done
            # by the model fitting itself
            X = atleast2d_or_csc(X, copy=False)
            if sparse.isspmatrix(X):
                if not np.may_share_memory(reference_to_old_X.data, X.data):
                    # X is a sparse matrix and has been copied
                    copy_X = False
            elif not np.may_share_memory(reference_to_old_X, X):
                # X has been copied
                copy_X = False
            del reference_to_old_X
        else:
            X = atleast2d_or_csc(X, dtype=np.float64, order='F',
                                 copy=copy_X)
            copy_X = False

        y = np.asarray(y, dtype=np.float64)

        if y.ndim > 1:
            raise ValueError("For multi-task outputs, fit the linear model "
                             "per output/task")
        if X.shape[0] != y.shape[0]:
            raise ValueError("X and y have inconsistent dimensions (%d != %d)"
                             % (X.shape[0], y.shape[0]))

        # All LinearModelCV parameters except 'cv' are acceptable
        path_params = self.get_params()
        if 'l1_ratio' in path_params:
            l1_ratios = np.atleast_1d(path_params['l1_ratio'])
            # For the first path, we need to set l1_ratio
            path_params['l1_ratio'] = l1_ratios[0]
        else:
            l1_ratios = [1, ]
        path_params.pop('cv', None)
        path_params.pop('n_jobs', None)

        alphas = self.alphas
        if alphas is None:
            mean_l1_ratio = 1.
            if hasattr(self, 'l1_ratio'):
                mean_l1_ratio = np.mean(self.l1_ratio)
            alphas = _alpha_grid(X, y, l1_ratio=mean_l1_ratio,
                                 fit_intercept=self.fit_intercept,
                                 eps=self.eps, n_alphas=self.n_alphas,
                                 normalize=self.normalize,
                                 copy_X=self.copy_X)
        n_alphas = len(alphas)
        path_params.update({'alphas': alphas, 'n_alphas': n_alphas})

        path_params['copy_X'] = copy_X
        # We are not computing in parallel, we can modify X
        # inplace in the folds
        if not (self.n_jobs == 1 or self.n_jobs is None):
            path_params['copy_X'] = False

        # init cross-validation generator
        cv = check_cv(self.cv, X)

        # Compute path for all folds and compute MSE to get the best alpha
        folds = list(cv)
        best_mse = np.inf
        all_mse_paths = list()

        # We do a double for loop folded in one, in order to be able to
        # iterate in parallel on l1_ratio and folds
        for l1_ratio, mse_alphas in itertools.groupby(
                Parallel(n_jobs=self.n_jobs, verbose=self.verbose)(
                    delayed(_path_residuals)(
                        X, y, train, test, self.path, path_params,
                        l1_ratio=l1_ratio, X_order='F',
                        dtype=np.float64)
                    for l1_ratio in l1_ratios for train, test in folds
                ), operator.itemgetter(1)):

            mse_alphas = [m[0] for m in mse_alphas]
            mse_alphas = np.array(mse_alphas)

            mse = np.mean(mse_alphas, axis=0)
            i_best_alpha = np.argmin(mse)
            this_best_mse = mse[i_best_alpha]
            all_mse_paths.append(mse_alphas.T)
            if this_best_mse < best_mse:
                best_alpha = alphas[i_best_alpha]
                best_l1_ratio = l1_ratio
                best_mse = this_best_mse

        self.l1_ratio_ = best_l1_ratio
        self.alpha_ = best_alpha
        self.alphas_ = np.asarray(alphas)
        self.mse_path_ = np.squeeze(all_mse_paths)

        # Refit the model with the parameters selected
        model = ElasticNet()
        common_params = dict((name, value)
                             for name, value in self.get_params().items()
                             if name in model.get_params())
        model.set_params(**common_params)
        model.alpha = best_alpha
        model.l1_ratio = best_l1_ratio
        model.copy_X = copy_X
        model.fit(X, y)
        self.coef_ = model.coef_
        self.intercept_ = model.intercept_
        self.dual_gap_ = model.dual_gap_
        return self


class LassoCV(LinearModelCV, RegressorMixin):
    """Lasso linear model with iterative fitting along a regularization path

    The best model is selected by cross-validation.

    The optimization objective for Lasso is::

        (1 / (2 * n_samples)) * ||y - Xw||^2_2 + alpha * ||w||_1

    Parameters
    ----------
    eps : float, optional
        Length of the path. ``eps=1e-3`` means that
        ``alpha_min / alpha_max = 1e-3``.

    n_alphas : int, optional
        Number of alphas along the regularization path

    alphas : numpy array, optional
        List of alphas where to compute the models.
        If ``None`` alphas are set automatically

    precompute : True | False | 'auto' | array-like
        Whether to use a precomputed Gram matrix to speed up
        calculations. If set to ``'auto'`` let us decide. The Gram
        matrix can also be passed as argument.

    max_iter: int, optional
        The maximum number of iterations

    tol: float, optional
        The tolerance for the optimization: if the updates are
        smaller than ``tol``, the optimization code checks the
        dual gap for optimality and continues until it is smaller
        than ``tol``.

    cv : integer or cross-validation generator, optional
        If an integer is passed, it is the number of fold (default 3).
        Specific cross-validation objects can be passed, see the
        :mod:`sklearn.cross_validation` module for the list of possible
        objects.

    verbose : bool or integer
        amount of verbosity

    Attributes
    ----------
    ``alpha_`` : float
        The amount of penalization chosen by cross validation

    ``coef_`` : array, shape = (n_features,) | (n_targets, n_features)
        parameter vector (w in the cost function formula)

    ``intercept_`` : float | array, shape = (n_targets,)
        independent term in decision function.

    ``mse_path_`` : array, shape = (n_alphas, n_folds)
        mean square error for the test set on each fold, varying alpha

    ``alphas_`` : numpy array
        The grid of alphas used for fitting

    ``l1_ratio_`` : int
        An artifact of the super class LinearModelCV. In this case,
        ``l1_ratio_ = 1`` because the Lasso estimator uses an L1 penalty
        by definition.
        Typically it is a float between 0 and 1 passed to an estimator such as
        ElasticNet (scaling between l1 and l2 penalties). For ``l1_ratio = 0``
        the penalty is an L2 penalty. For ``l1_ratio = 1`` it is an L1 penalty.

    ``dual_gap_`` : numpy array
        The dual gap at the end of the optimization for the optimal alpha
        (``alpha_``).

    Notes
    -----
    See examples/linear_model/lasso_path_with_crossvalidation.py
    for an example.

    To avoid unnecessary memory duplication the X argument of the fit method
    should be directly passed as a Fortran-contiguous numpy array.

    See also
    --------
    lars_path
    lasso_path
    LassoLars
    Lasso
    LassoLarsCV
    """
    path = staticmethod(lasso_path)
    n_jobs = 1

    def __init__(self, eps=1e-3, n_alphas=100, alphas=None, fit_intercept=True,
                 normalize=False, precompute='auto', max_iter=1000, tol=1e-4,
                 copy_X=True, cv=None, verbose=False):
        super(LassoCV, self).__init__(
            eps=eps, n_alphas=n_alphas, alphas=alphas,
            fit_intercept=fit_intercept, normalize=normalize,
            precompute=precompute, max_iter=max_iter, tol=tol, copy_X=copy_X,
            cv=cv, verbose=verbose)


class ElasticNetCV(LinearModelCV, RegressorMixin):
    """Elastic Net model with iterative fitting along a regularization path

    The best model is selected by cross-validation.

    Parameters
    ----------
    l1_ratio : float, optional
        float between 0 and 1 passed to ElasticNet (scaling between
        l1 and l2 penalties). For ``l1_ratio = 0``
        the penalty is an L2 penalty. For ``l1_ratio = 1`` it is an L1 penalty.
        For ``0 < l1_ratio < 1``, the penalty is a combination of L1 and L2
        This parameter can be a list, in which case the different
        values are tested by cross-validation and the one giving the best
        prediction score is used. Note that a good choice of list of
        values for l1_ratio is often to put more values close to 1
        (i.e. Lasso) and less close to 0 (i.e. Ridge), as in ``[.1, .5, .7,
        .9, .95, .99, 1]``

    eps : float, optional
        Length of the path. ``eps=1e-3`` means that
        ``alpha_min / alpha_max = 1e-3``.

    n_alphas : int, optional
        Number of alphas along the regularization path

    alphas : numpy array, optional
        List of alphas where to compute the models.
        If None alphas are set automatically

    precompute : True | False | 'auto' | array-like
        Whether to use a precomputed Gram matrix to speed up
        calculations. If set to ``'auto'`` let us decide. The Gram
        matrix can also be passed as argument.

    max_iter : int, optional
        The maximum number of iterations

    tol : float, optional
        The tolerance for the optimization: if the updates are
        smaller than ``tol``, the optimization code checks the
        dual gap for optimality and continues until it is smaller
        than ``tol``.

    cv : integer or cross-validation generator, optional
        If an integer is passed, it is the number of fold (default 3).
        Specific cross-validation objects can be passed, see the
        :mod:`sklearn.cross_validation` module for the list of possible
        objects.

    verbose : bool or integer
        amount of verbosity

    n_jobs : integer, optional
        Number of CPUs to use during the cross validation. If ``-1``, use
        all the CPUs. Note that this is used only if multiple values for
        l1_ratio are given.

    Attributes
    ----------
    ``alpha_`` : float
        The amount of penalization chosen by cross validation

    ``l1_ratio_`` : float
        The compromise between l1 and l2 penalization chosen by
        cross validation

    ``coef_`` : array, shape = (n_features,) | (n_targets, n_features)
        Parameter vector (w in the cost function formula),

    ``intercept_`` : float | array, shape = (n_targets, n_features)
        Independent term in the decision function.

    ``mse_path_`` : array, shape = (n_l1_ratio, n_alpha, n_folds)
        Mean square error for the test set on each fold, varying l1_ratio and
        alpha.

    Notes
    -----
    See examples/linear_model/lasso_path_with_crossvalidation.py
    for an example.

    To avoid unnecessary memory duplication the X argument of the fit method
    should be directly passed as a Fortran-contiguous numpy array.

    The parameter l1_ratio corresponds to alpha in the glmnet R package
    while alpha corresponds to the lambda parameter in glmnet.
    More specifically, the optimization objective is::

        1 / (2 * n_samples) * ||y - Xw||^2_2 +
        + alpha * l1_ratio * ||w||_1
        + 0.5 * alpha * (1 - l1_ratio) * ||w||^2_2

    If you are interested in controlling the L1 and L2 penalty
    separately, keep in mind that this is equivalent to::

        a * L1 + b * L2

    for::

        alpha = a + b and l1_ratio = a / (a + b).

    See also
    --------
    enet_path
    ElasticNet

    """
    path = staticmethod(enet_path)

    def __init__(self, l1_ratio=0.5, eps=1e-3, n_alphas=100, alphas=None,
                 fit_intercept=True, normalize=False, precompute='auto',
                 max_iter=1000, tol=1e-4, cv=None, copy_X=True,
                 verbose=0, n_jobs=1):
        self.l1_ratio = l1_ratio
        self.eps = eps
        self.n_alphas = n_alphas
        self.alphas = alphas
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.precompute = precompute
        self.max_iter = max_iter
        self.tol = tol
        self.cv = cv
        self.copy_X = copy_X
        self.verbose = verbose
        self.n_jobs = n_jobs


###############################################################################
# Multi Task ElasticNet and Lasso models (with joint feature selection)

class MultiTaskElasticNet(Lasso):
    """Multi-task ElasticNet model trained with L1/L2 mixed-norm as regularizer

    The optimization objective for Lasso is::

        (1 / (2 * n_samples)) * ||Y - XW||^Fro_2
        + alpha * l1_ratio * ||W||_21
        + 0.5 * alpha * (1 - l1_ratio) * ||W||_Fro^2

    Where::

        ||W||_21 = \sum_i \sqrt{\sum_j w_{ij}^2}

    i.e. the sum of norm of earch row.

    Parameters
    ----------
    alpha : float, optional
        Constant that multiplies the L1/L2 term. Defaults to 1.0

    l1_ratio : float
        The ElasticNet mixing parameter, with 0 < l1_ratio <= 1.
        For l1_ratio = 0 the penalty is an L1/L2 penalty. For l1_ratio = 1 it
        is an L1 penalty.
        For ``0 < l1_ratio < 1``, the penalty is a combination of L1/L2 and L2.

    fit_intercept : boolean
        whether to calculate the intercept for this model. If set
        to false, no intercept will be used in calculations
        (e.g. data is expected to be already centered).

    normalize : boolean, optional, default False
        If ``True``, the regressors X will be normalized before regression.

    copy_X : boolean, optional, default True
        If ``True``, X will be copied; else, it may be overwritten.

    max_iter : int, optional
        The maximum number of iterations

    tol : float, optional
        The tolerance for the optimization: if the updates are
        smaller than ``tol``, the optimization code checks the
        dual gap for optimality and continues until it is smaller
        than ``tol``.

    warm_start : bool, optional
        When set to ``True``, reuse the solution of the previous call to fit as
        initialization, otherwise, just erase the previous solution.

    Attributes
    ----------
    ``intercept_`` : array, shape = (n_tasks,)
        Independent term in decision function.

    ``coef_`` : array, shape = (n_tasks, n_features)
        Parameter vector (W in the cost function formula). If a 1D y is \
        passed in at fit (non multi-task usage), ``coef_`` is then a 1D array

    Examples
    --------
    >>> from sklearn import linear_model
    >>> clf = linear_model.MultiTaskElasticNet(alpha=0.1)
    >>> clf.fit([[0,0], [1, 1], [2, 2]], [[0, 0], [1, 1], [2, 2]])
    ... #doctest: +NORMALIZE_WHITESPACE
    MultiTaskElasticNet(alpha=0.1, copy_X=True, fit_intercept=True,
            l1_ratio=0.5, max_iter=1000, normalize=False, tol=0.0001,
            warm_start=False)
    >>> print(clf.coef_)
    [[ 0.45663524  0.45612256]
     [ 0.45663524  0.45612256]]
    >>> print(clf.intercept_)
    [ 0.0872422  0.0872422]

    See also
    --------
    ElasticNet, MultiTaskLasso

    Notes
    -----
    The algorithm used to fit the model is coordinate descent.

    To avoid unnecessary memory duplication the X argument of the fit method
    should be directly passed as a Fortran-contiguous numpy array.
    """
    def __init__(self, alpha=1.0, l1_ratio=0.5, fit_intercept=True,
                 normalize=False, copy_X=True, max_iter=1000, tol=1e-4,
                 warm_start=False):
        self.l1_ratio = l1_ratio
        self.alpha = alpha
        self.coef_ = None
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.max_iter = max_iter
        self.copy_X = copy_X
        self.tol = tol
        self.warm_start = warm_start

    def fit(self, X, y):
        """Fit MultiTaskLasso model with coordinate descent

        Parameters
        -----------
        X: ndarray, shape = (n_samples, n_features)
            Data
        y: ndarray, shape = (n_samples, n_tasks)
            Target

        Notes
        -----

        Coordinate descent is an algorithm that considers each column of
        data at a time hence it will automatically convert the X input
        as a Fortran-contiguous numpy array if necessary.

        To avoid memory re-allocation it is advised to allocate the
        initial data in memory directly using that format.
        """
        # X and y must be of type float64
        X = array2d(X, dtype=np.float64, order='F',
                    copy=self.copy_X and self.fit_intercept)
        y = np.asarray(y, dtype=np.float64)

        squeeze_me = False
        if y.ndim == 1:
            squeeze_me = True
            y = y[:, np.newaxis]

        n_samples, n_features = X.shape
        _, n_tasks = y.shape

        X, y, X_mean, y_mean, X_std = center_data(
            X, y, self.fit_intercept, self.normalize, copy=False)

        if not self.warm_start or self.coef_ is None:
            self.coef_ = np.zeros((n_tasks, n_features), dtype=np.float64,
                                  order='F')

        l1_reg = self.alpha * self.l1_ratio * n_samples
        l2_reg = self.alpha * (1.0 - self.l1_ratio) * n_samples

        self.coef_ = np.asfortranarray(self.coef_)  # coef contiguous in memory

        self.coef_, self.dual_gap_, self.eps_ = \
            cd_fast.enet_coordinate_descent_multi_task(
                self.coef_, l1_reg, l2_reg, X, y, self.max_iter, self.tol)

        self._set_intercept(X_mean, y_mean, X_std)

        # Make sure that the coef_ have the same shape as the given 'y',
        # to predict with the same shape
        if squeeze_me:
            self.coef_ = self.coef_.squeeze()

        if self.dual_gap_ > self.eps_:
            warnings.warn('Objective did not converge, you might want'
                          ' to increase the number of iterations')

        # return self for chaining fit and predict calls
        return self


class MultiTaskLasso(MultiTaskElasticNet):
    """Multi-task Lasso model trained with L1/L2 mixed-norm as regularizer

    The optimization objective for Lasso is::

        (1 / (2 * n_samples)) * ||Y - XW||^2_Fro + alpha * ||W||_21

    Where::

        ||W||_21 = \sum_i \sqrt{\sum_j w_{ij}^2}

    i.e. the sum of norm of earch row.

    Parameters
    ----------
    alpha : float, optional
        Constant that multiplies the L1/L2 term. Defaults to 1.0

    fit_intercept : boolean
        whether to calculate the intercept for this model. If set
        to false, no intercept will be used in calculations
        (e.g. data is expected to be already centered).

    normalize : boolean, optional, default False
        If ``True``, the regressors X will be normalized before regression.

    copy_X : boolean, optional, default True
        If ``True``, X will be copied; else, it may be overwritten.

    max_iter : int, optional
        The maximum number of iterations

    tol : float, optional
        The tolerance for the optimization: if the updates are
        smaller than ``tol``, the optimization code checks the
        dual gap for optimality and continues until it is smaller
        than ``tol``.

    warm_start : bool, optional
        When set to ``True``, reuse the solution of the previous call to fit as
        initialization, otherwise, just erase the previous solution.

    Attributes
    ----------
    ``coef_`` : array, shape = (n_tasks, n_features)
        parameter vector (W in the cost function formula)

    ``intercept_`` : array, shape = (n_tasks,)
        independent term in decision function.

    Examples
    --------
    >>> from sklearn import linear_model
    >>> clf = linear_model.MultiTaskLasso(alpha=0.1)
    >>> clf.fit([[0,0], [1, 1], [2, 2]], [[0, 0], [1, 1], [2, 2]])
    MultiTaskLasso(alpha=0.1, copy_X=True, fit_intercept=True, max_iter=1000,
            normalize=False, tol=0.0001, warm_start=False)
    >>> print(clf.coef_)
    [[ 0.89393398  0.        ]
     [ 0.89393398  0.        ]]
    >>> print(clf.intercept_)
    [ 0.10606602  0.10606602]

    See also
    --------
    Lasso, MultiTaskElasticNet

    Notes
    -----
    The algorithm used to fit the model is coordinate descent.

    To avoid unnecessary memory duplication the X argument of the fit method
    should be directly passed as a Fortran-contiguous numpy array.
    """
    def __init__(self, alpha=1.0, fit_intercept=True, normalize=False,
                 copy_X=True, max_iter=1000, tol=1e-4, warm_start=False):
        self.alpha = alpha
        self.coef_ = None
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.max_iter = max_iter
        self.copy_X = copy_X
        self.tol = tol
        self.warm_start = warm_start
        self.l1_ratio = 1.0
