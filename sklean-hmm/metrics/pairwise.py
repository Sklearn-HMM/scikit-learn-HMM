# -*- coding: utf-8 -*-
"""
The :mod:`sklearn.metrics.pairwise` submodule implements utilities to evaluate
pairwise distances or affinity of sets of samples.

This module contains both distance metrics and kernels. A brief summary is
given on the two here.

Distance metrics are a function d(a, b) such that d(a, b) < d(a, c) if objects
a and b are considered "more similar" to objects a and c. Two objects exactly
alike would have a distance of zero.
One of the most popular examples is Euclidean distance.
To be a 'true' metric, it must obey the following four conditions::

    1. d(a, b) >= 0, for all a and b
    2. d(a, b) == 0, if and only if a = b, positive definiteness
    3. d(a, b) == d(b, a), symmetry
    4. d(a, c) <= d(a, b) + d(b, c), the triangle inequality

Kernels are measures of similarity, i.e. ``s(a, b) > s(a, c)``
if objects ``a`` and ``b`` are considered "more similar" to objects
``a`` and ``c``. A kernel must also be positive semi-definite.

There are a number of ways to convert between a distance metric and a
similarity measure, such as a kernel. Let D be the distance, and S be the
kernel:

    1. ``S = np.exp(-D * gamma)``, where one heuristic for choosing
       ``gamma`` is ``1 / num_features``
    2. ``S = 1. / (D / np.max(D))``
"""

# Authors: Alexandre Gramfort <alexandre.gramfort@inria.fr>
#          Mathieu Blondel <mathieu@mblondel.org>
#          Robert Layton <robertlayton@gmail.com>
#          Andreas Mueller <amueller@ais.uni-bonn.de>
#          Philippe Gervais <philippe.gervais@inria.fr>
#          Lars Buitinck <larsmans@gmail.com>
# License: BSD 3 clause

import numpy as np
from scipy.spatial import distance
from scipy.sparse import csr_matrix
from scipy.sparse import issparse

from ..utils import array2d, atleast2d_or_csr
from ..utils import gen_even_slices
from ..utils import gen_batches
from ..utils import safe_asarray
from ..utils.extmath import row_norms, safe_sparse_dot
from ..preprocessing import normalize
from ..externals.joblib import Parallel
from ..externals.joblib import delayed
from ..externals.joblib.parallel import cpu_count

from .pairwise_fast import _chi2_kernel_fast


# Utility Functions
def check_pairwise_arrays(X, Y):
    """ Set X and Y appropriately and checks inputs

    If Y is None, it is set as a pointer to X (i.e. not a copy).
    If Y is given, this does not happen.
    All distance metrics should use this function first to assert that the
    given parameters are correct and safe to use.

    Specifically, this function first ensures that both X and Y are arrays,
    then checks that they are at least two dimensional while ensuring that
    their elements are floats. Finally, the function checks that the size
    of the second dimension of the two arrays is equal.

    Parameters
    ----------
    X : {array-like, sparse matrix}, shape = [n_samples_a, n_features]

    Y : {array-like, sparse matrix}, shape = [n_samples_b, n_features]

    Returns
    -------
    safe_X : {array-like, sparse matrix}, shape = [n_samples_a, n_features]
        An array equal to X, guaranteed to be a numpy array.

    safe_Y : {array-like, sparse matrix}, shape = [n_samples_b, n_features]
        An array equal to Y if Y was not None, guaranteed to be a numpy array.
        If Y was None, safe_Y will be a pointer to X.

    """
    if Y is X or Y is None:
        X = Y = atleast2d_or_csr(X)
    else:
        X = atleast2d_or_csr(X)
        Y = atleast2d_or_csr(Y)
    if X.shape[1] != Y.shape[1]:
        raise ValueError("Incompatible dimension for X and Y matrices: "
                         "X.shape[1] == %d while Y.shape[1] == %d" % (
                             X.shape[1], Y.shape[1]))

    if not (X.dtype == Y.dtype == np.float32):
        if Y is X:
            X = Y = safe_asarray(X, dtype=np.float)
        else:
            X = safe_asarray(X, dtype=np.float)
            Y = safe_asarray(Y, dtype=np.float)
    return X, Y


# Distances
def euclidean_distances(X, Y=None, Y_norm_squared=None, squared=False):
    """
    Considering the rows of X (and Y=X) as vectors, compute the
    distance matrix between each pair of vectors.

    For efficiency reasons, the euclidean distance between a pair of row
    vector x and y is computed as::

        dist(x, y) = sqrt(dot(x, x) - 2 * dot(x, y) + dot(y, y))

    This formulation has two main advantages. First, it is computationally
    efficient when dealing with sparse data. Second, if x varies but y
    remains unchanged, then the right-most dot-product `dot(y, y)` can be
    pre-computed.

    Parameters
    ----------
    X : {array-like, sparse matrix}, shape = [n_samples_1, n_features]

    Y : {array-like, sparse matrix}, shape = [n_samples_2, n_features]

    Y_norm_squared : array-like, shape = [n_samples_2], optional
        Pre-computed dot-products of vectors in Y (e.g.,
        ``(Y**2).sum(axis=1)``)

    squared : boolean, optional
        Return squared Euclidean distances.

    Returns
    -------
    distances : {array, sparse matrix}, shape = [n_samples_1, n_samples_2]

    Examples
    --------
    >>> from sklearn.metrics.pairwise import euclidean_distances
    >>> X = [[0, 1], [1, 1]]
    >>> # distance between rows of X
    >>> euclidean_distances(X, X)
    array([[ 0.,  1.],
           [ 1.,  0.]])
    >>> # get distance to origin
    >>> euclidean_distances(X, [[0, 0]])
    array([[ 1.        ],
           [ 1.41421356]])
    """
    # should not need X_norm_squared because if you could precompute that as
    # well as Y, then you should just pre-compute the output and not even
    # call this function.
    X, Y = check_pairwise_arrays(X, Y)

    if Y_norm_squared is not None:
        YY = array2d(Y_norm_squared)
        if YY.shape != (1, Y.shape[0]):
            raise ValueError(
                "Incompatible dimensions for Y and Y_norm_squared")
    else:
        YY = row_norms(Y, squared=True)[np.newaxis, :]

    if X is Y:  # shortcut in the common case euclidean_distances(X, X)
        XX = YY.T
    else:
        XX = row_norms(X, squared=True)[:, np.newaxis]

    distances = safe_sparse_dot(X, Y.T, dense_output=True)
    distances *= -2
    distances += XX
    distances += YY
    np.maximum(distances, 0, distances)

    if X is Y:
        # Ensure that distances between vectors and themselves are set to 0.0.
        # This may not be the case due to floating point rounding errors.
        distances.flat[::distances.shape[0] + 1] = 0.0

    return distances if squared else np.sqrt(distances)


def pairwise_distances_argmin_min(X, Y, axis=1, metric="euclidean",
                                  batch_size=500, metric_kwargs=None):
    """Compute minimum distances between one point and a set of points.

    This function computes for each row in X, the index of the row of Y which
    is closest (according to the specified distance). The minimal distances are
    also returned.

    This is mostly equivalent to calling:

        (pairwise_distances(X, Y=Y, metric=metric).argmin(axis=axis),
         pairwise_distances(X, Y=Y, metric=metric).min(axis=axis))

    but uses much less memory, and is faster for large arrays.

    Parameters
    ----------
    X, Y : {array-like, sparse matrix}
        Arrays containing points. Respective shapes (n_samples1, n_features)
        and (n_samples2, n_features)

    batch_size : integer
        To reduce memory consumption over the naive solution, data are
        processed in batches, comprising batch_size rows of X and
        batch_size rows of Y. The default value is quite conservative, but
        can be changed for fine-tuning. The larger the number, the larger the
        memory usage.

    metric : string or callable
        metric to use for distance computation. Any metric from scikit-learn
        or scipy.spatial.distance can be used.

        If metric is a callable function, it is called on each
        pair of instances (rows) and the resulting value recorded. The callable
        should take two arrays as input and return one value indicating the
        distance between them. This works for Scipy's metrics, but is less
        efficient than passing the metric name as a string.

        Distance matrices are not supported.

        Valid values for metric are:

        - from scikit-learn: ['cityblock', 'cosine', 'euclidean', 'l1', 'l2',
        'manhattan']

        - from scipy.spatial.distance: ['braycurtis', 'canberra', 'chebyshev',
        'correlation', 'dice', 'hamming', 'jaccard', 'kulsinski',
        'mahalanobis', 'matching', 'minkowski', 'rogerstanimoto', 'russellrao',
        'seuclidean', 'sokalmichener', 'sokalsneath', 'sqeuclidean', 'yule']
        See the documentation for scipy.spatial.distance for details on these
        metrics.

    metric_kwargs : dict, optional
        Keyword arguments to pass to specified metric function.

    Returns
    -------
    argmin : numpy.ndarray
        Y[argmin[i], :] is the row in Y that is closest to X[i, :].

    distances : numpy.ndarray
        distances[i] is the distance between the i-th row in X and the
        argmin[i]-th row in Y.

    See also
    --------
    sklearn.metrics.pairwise_distances
    sklearn.metrics.pairwise_distances_argmin
    """
    dist_func = None
    if metric in PAIRWISE_DISTANCE_FUNCTIONS:
        dist_func = PAIRWISE_DISTANCE_FUNCTIONS[metric]
    elif not callable(metric) and not isinstance(metric, str):
        raise ValueError("'metric' must be a string or a callable")

    X, Y = check_pairwise_arrays(X, Y)

    if metric_kwargs is None:
        metric_kwargs = {}

    if axis == 0:
        X, Y = Y, X

    # Allocate output arrays
    indices = np.empty(X.shape[0], dtype=np.intp)
    values = np.empty(X.shape[0])
    values.fill(np.infty)

    for chunk_x in gen_batches(X.shape[0], batch_size):
        X_chunk = X[chunk_x, :]

        for chunk_y in gen_batches(Y.shape[0], batch_size):
            Y_chunk = Y[chunk_y, :]

            if dist_func is not None:
                if metric == 'euclidean':  # special case, for speed
                    d_chunk = safe_sparse_dot(X_chunk, Y_chunk.T,
                                                 dense_output=True)
                    d_chunk *= -2
                    d_chunk += row_norms(X_chunk, squared=True)[:, np.newaxis]
                    d_chunk += row_norms(Y_chunk, squared=True)[np.newaxis, :]
                    np.maximum(d_chunk, 0, d_chunk)
                else:
                    d_chunk = dist_func(X_chunk, Y_chunk, **metric_kwargs)
            else:
                d_chunk = pairwise_distances(X_chunk, Y_chunk,
                                             metric=metric, **metric_kwargs)

            # Update indices and minimum values using chunk
            min_indices = d_chunk.argmin(axis=1)
            min_values = d_chunk[np.arange(chunk_x.stop - chunk_x.start),
                                 min_indices]

            flags = values[chunk_x] > min_values
            indices[chunk_x] = np.where(
                flags, min_indices + chunk_y.start, indices[chunk_x])

            values[chunk_x] = np.where(
                flags, min_values, values[chunk_x])

    if metric == "euclidean" and not metric_kwargs.get("squared", False):
        np.sqrt(values, values)
    return indices, values


def pairwise_distances_argmin(X, Y, axis=1, metric="euclidean",
                              batch_size=500, metric_kwargs={}):
    """Compute minimum distances between one point and a set of points.

    This function computes for each row in X, the index of the row of Y which
    is closest (according to the specified distance).

    This is mostly equivalent to calling:

        pairwise_distances(X, Y=Y, metric=metric).argmin(axis=axis)

    but uses much less memory, and is faster for large arrays.

    This function works with dense 2D arrays only.

    Parameters
    ==========
    X, Y : array-like
        Arrays containing points. Respective shapes (n_samples1, n_features)
        and (n_samples2, n_features)

    batch_size : integer
        To reduce memory consumption over the naive solution, data are
        processed in batches, comprising batch_size rows of X and
        batch_size rows of Y. The default value is quite conservative, but
        can be changed for fine-tuning. The larger the number, the larger the
        memory usage.

    metric : string or callable
        metric to use for distance computation. Any metric from scikit-learn
        or scipy.spatial.distance can be used.

        If metric is a callable function, it is called on each
        pair of instances (rows) and the resulting value recorded. The callable
        should take two arrays as input and return one value indicating the
        distance between them. This works for Scipy's metrics, but is less
        efficient than passing the metric name as a string.

        Distance matrices are not supported.

        Valid values for metric are:

        - from scikit-learn: ['cityblock', 'cosine', 'euclidean', 'l1', 'l2',
        'manhattan']

        - from scipy.spatial.distance: ['braycurtis', 'canberra', 'chebyshev',
        'correlation', 'dice', 'hamming', 'jaccard', 'kulsinski',
        'mahalanobis', 'matching', 'minkowski', 'rogerstanimoto', 'russellrao',
        'seuclidean', 'sokalmichener', 'sokalsneath', 'sqeuclidean', 'yule']
        See the documentation for scipy.spatial.distance for details on these
        metrics.

    metric_kwargs : dict
        keyword arguments to pass to specified metric function.

    Returns
    =======
    argmin : numpy.ndarray
        Y[argmin[i], :] is the row in Y that is closest to X[i, :].

    See also
    ========
    sklearn.metrics.pairwise_distances
    sklearn.metrics.pairwise_distances_argmin_min
    """
    return pairwise_distances_argmin_min(X, Y, axis, metric, batch_size,
                                         metric_kwargs)[0]


def manhattan_distances(X, Y=None, sum_over_features=True,
                        size_threshold=5e8):
    """ Compute the L1 distances between the vectors in X and Y.

    With sum_over_features equal to False it returns the componentwise
    distances.

    Parameters
    ----------
    X : array_like
        An array with shape (n_samples_X, n_features).

    Y : array_like, optional
        An array with shape (n_samples_Y, n_features).

    sum_over_features : bool, default=True
        If True the function returns the pairwise distance matrix
        else it returns the componentwise L1 pairwise-distances.

    size_threshold : int, default=5e8
        Avoid creating temporary matrices bigger than size_threshold (in
        bytes). If the problem size gets too big, the implementation then
        breaks it down in smaller problems.

    Returns
    -------
    D : array
        If sum_over_features is False shape is
        (n_samples_X * n_samples_Y, n_features) and D contains the
        componentwise L1 pairwise-distances (ie. absolute difference),
        else shape is (n_samples_X, n_samples_Y) and D contains
        the pairwise l1 distances.

    Examples
    --------
    >>> from sklearn.metrics.pairwise import manhattan_distances
    >>> manhattan_distances(3, 3)#doctest:+ELLIPSIS
    array([[ 0.]])
    >>> manhattan_distances(3, 2)#doctest:+ELLIPSIS
    array([[ 1.]])
    >>> manhattan_distances(2, 3)#doctest:+ELLIPSIS
    array([[ 1.]])
    >>> manhattan_distances([[1, 2], [3, 4]],\
         [[1, 2], [0, 3]])#doctest:+ELLIPSIS
    array([[ 0.,  2.],
           [ 4.,  4.]])
    >>> import numpy as np
    >>> X = np.ones((1, 2))
    >>> y = 2 * np.ones((2, 2))
    >>> manhattan_distances(X, y, sum_over_features=False)#doctest:+ELLIPSIS
    array([[ 1.,  1.],
           [ 1.,  1.]]...)
    """
    if issparse(X) or issparse(Y):
        raise ValueError("manhattan_distance does not support sparse"
                         " matrices.")
    X, Y = check_pairwise_arrays(X, Y)
    temporary_size = X.size * Y.shape[-1]
    # Convert to bytes
    temporary_size *= X.itemsize
    if temporary_size > size_threshold and sum_over_features:
        # Broadcasting the full thing would be too big: it's on the order
        # of magnitude of the gigabyte
        D = np.empty((X.shape[0], Y.shape[0]), dtype=X.dtype)
        index = 0
        increment = 1 + int(size_threshold / float(temporary_size) *
                            X.shape[0])
        while index < X.shape[0]:
            this_slice = slice(index, index + increment)
            tmp = X[this_slice, np.newaxis, :] - Y[np.newaxis, :, :]
            tmp = np.abs(tmp, tmp)
            tmp = np.sum(tmp, axis=2)
            D[this_slice] = tmp
            index += increment
    else:
        D = X[:, np.newaxis, :] - Y[np.newaxis, :, :]
        D = np.abs(D, D)
        if sum_over_features:
            D = np.sum(D, axis=2)
        else:
            D = D.reshape((-1, X.shape[1]))
    return D


def cosine_distances(X, Y=None):
    """
    Compute cosine distance between samples in X and Y.

    Cosine distance is defined as 1.0 minus the cosine similarity.

    Parameters
    ----------
    X : array_like, sparse matrix
        with shape (n_samples_X, n_features).

    Y : array_like, sparse matrix (optional)
        with shape (n_samples_Y, n_features).

    Returns
    -------
    distance matrix : array
        An array with shape (n_samples_X, n_samples_Y).

    See also
    --------
    sklearn.metrics.pairwise.cosine_similarity
    scipy.spatial.distance.cosine (dense matrices only)
    """
    # 1.0 - cosine_similarity(X, Y) without copy
    S = cosine_similarity(X, Y)
    S *= -1
    S += 1
    return S


# Kernels
def linear_kernel(X, Y=None):
    """
    Compute the linear kernel between X and Y.

    Parameters
    ----------
    X : array of shape (n_samples_1, n_features)

    Y : array of shape (n_samples_2, n_features)

    Returns
    -------
    Gram matrix : array of shape (n_samples_1, n_samples_2)
    """
    X, Y = check_pairwise_arrays(X, Y)
    return safe_sparse_dot(X, Y.T, dense_output=True)


def polynomial_kernel(X, Y=None, degree=3, gamma=None, coef0=1):
    """
    Compute the polynomial kernel between X and Y::

        K(X, Y) = (gamma <X, Y> + coef0)^degree

    Parameters
    ----------
    X : array of shape (n_samples_1, n_features)

    Y : array of shape (n_samples_2, n_features)

    degree : int

    Returns
    -------
    Gram matrix : array of shape (n_samples_1, n_samples_2)
    """
    X, Y = check_pairwise_arrays(X, Y)
    if gamma is None:
        gamma = 1.0 / X.shape[1]

    K = linear_kernel(X, Y)
    K *= gamma
    K += coef0
    K **= degree
    return K


def sigmoid_kernel(X, Y=None, gamma=None, coef0=1):
    """
    Compute the sigmoid kernel between X and Y::

        K(X, Y) = tanh(gamma <X, Y> + coef0)

    Parameters
    ----------
    X : array of shape (n_samples_1, n_features)

    Y : array of shape (n_samples_2, n_features)

    degree : int

    Returns
    -------
    Gram matrix: array of shape (n_samples_1, n_samples_2)
    """
    X, Y = check_pairwise_arrays(X, Y)
    if gamma is None:
        gamma = 1.0 / X.shape[1]

    K = linear_kernel(X, Y)
    K *= gamma
    K += coef0
    np.tanh(K, K)   # compute tanh in-place
    return K


def rbf_kernel(X, Y=None, gamma=None):
    """
    Compute the rbf (gaussian) kernel between X and Y::

        K(x, y) = exp(-gamma ||x-y||^2)

    for each pair of rows x in X and y in Y.

    Parameters
    ----------
    X : array of shape (n_samples_X, n_features)

    Y : array of shape (n_samples_Y, n_features)

    gamma : float

    Returns
    -------
    kernel_matrix : array of shape (n_samples_X, n_samples_Y)
    """
    X, Y = check_pairwise_arrays(X, Y)
    if gamma is None:
        gamma = 1.0 / X.shape[1]

    K = euclidean_distances(X, Y, squared=True)
    K *= -gamma
    np.exp(K, K)    # exponentiate K in-place
    return K


def cosine_similarity(X, Y=None):
    """Compute cosine similarity between samples in X and Y.

    Cosine similarity, or the cosine kernel, computes similarity as the
    normalized dot product of X and Y:

        K(X, Y) = <X, Y> / (||X||*||Y||)

    On L2-normalized data, this function is equivalent to linear_kernel.

    Parameters
    ----------
    X : array_like, sparse matrix
        with shape (n_samples_X, n_features).

    Y : array_like, sparse matrix (optional)
        with shape (n_samples_Y, n_features).

    Returns
    -------
    kernel matrix : array
        An array with shape (n_samples_X, n_samples_Y).
    """
    # to avoid recursive import

    X, Y = check_pairwise_arrays(X, Y)

    X_normalized = normalize(X, copy=True)
    if X is Y:
        Y_normalized = X_normalized
    else:
        Y_normalized = normalize(Y, copy=True)

    K = linear_kernel(X_normalized, Y_normalized)

    return K


def additive_chi2_kernel(X, Y=None):
    """Computes the additive chi-squared kernel between observations in X and Y

    The chi-squared kernel is computed between each pair of rows in X and Y.  X
    and Y have to be non-negative. This kernel is most commonly applied to
    histograms.

    The chi-squared kernel is given by::

        k(x, y) = -Sum [(x - y)^2 / (x + y)]

    It can be interpreted as a weighted difference per entry.

    Notes
    -----
    As the negative of a distance, this kernel is only conditionally positive
    definite.


    Parameters
    ----------
    X : array-like of shape (n_samples_X, n_features)

    Y : array of shape (n_samples_Y, n_features)

    Returns
    -------
    kernel_matrix : array of shape (n_samples_X, n_samples_Y)

    References
    ----------
    * Zhang, J. and Marszalek, M. and Lazebnik, S. and Schmid, C.
      Local features and kernels for classification of texture and object
      categories: A comprehensive study
      International Journal of Computer Vision 2007
      http://eprints.pascal-network.org/archive/00002309/01/Zhang06-IJCV.pdf


    See also
    --------
    chi2_kernel : The exponentiated version of the kernel, which is usually
        preferable.

    sklearn.kernel_approximation.AdditiveChi2Sampler : A Fourier approximation
        to this kernel.
    """
    if issparse(X) or issparse(Y):
        raise ValueError("additive_chi2 does not support sparse matrices.")
    X, Y = check_pairwise_arrays(X, Y)
    if (X < 0).any():
        raise ValueError("X contains negative values.")
    if Y is not X and (Y < 0).any():
        raise ValueError("Y contains negative values.")

    result = np.zeros((X.shape[0], Y.shape[0]), dtype=X.dtype)
    _chi2_kernel_fast(X, Y, result)
    return result


def chi2_kernel(X, Y=None, gamma=1.):
    """Computes the exponential chi-squared kernel X and Y.

    The chi-squared kernel is computed between each pair of rows in X and Y.  X
    and Y have to be non-negative. This kernel is most commonly applied to
    histograms.

    The chi-squared kernel is given by::

        k(x, y) = exp(-gamma Sum [(x - y)^2 / (x + y)])

    It can be interpreted as a weighted difference per entry.

    Parameters
    ----------
    X : array-like of shape (n_samples_X, n_features)

    Y : array of shape (n_samples_Y, n_features)

    gamma : float, default=1.
        Scaling parameter of the chi2 kernel.

    Returns
    -------
    kernel_matrix : array of shape (n_samples_X, n_samples_Y)

    References
    ----------
    * Zhang, J. and Marszalek, M. and Lazebnik, S. and Schmid, C.
      Local features and kernels for classification of texture and object
      categories: A comprehensive study
      International Journal of Computer Vision 2007
      http://eprints.pascal-network.org/archive/00002309/01/Zhang06-IJCV.pdf

    See also
    --------
    additive_chi2_kernel : The additive version of this kernel

    sklearn.kernel_approximation.AdditiveChi2Sampler : A Fourier approximation
        to the additive version of this kernel.
    """
    K = additive_chi2_kernel(X, Y)
    K *= gamma
    return np.exp(K, K)


# Helper functions - distance
PAIRWISE_DISTANCE_FUNCTIONS = {
    # If updating this dictionary, update the doc in both distance_metrics()
    # and also in pairwise_distances()!
    'cityblock': manhattan_distances,
    'cosine': cosine_distances,
    'euclidean': euclidean_distances,
    'l2': euclidean_distances,
    'l1': manhattan_distances,
    'manhattan': manhattan_distances, }


def distance_metrics():
    """Valid metrics for pairwise_distances.

    This function simply returns the valid pairwise distance metrics.
    It exists to allow for a description of the mapping for
    each of the valid strings.

    The valid distance metrics, and the function they map to, are:

    ============     ====================================
    metric           Function
    ============     ====================================
    'cityblock'      metrics.pairwise.manhattan_distances
    'cosine'         metrics.pairwise.cosine_distances
    'euclidean'      metrics.pairwise.euclidean_distances
    'l1'             metrics.pairwise.manhattan_distances
    'l2'             metrics.pairwise.euclidean_distances
    'manhattan'      metrics.pairwise.manhattan_distances
    ============     ====================================

    """
    return PAIRWISE_DISTANCE_FUNCTIONS


def _parallel_pairwise(X, Y, func, n_jobs, **kwds):
    """Break the pairwise matrix in n_jobs even slices
    and compute them in parallel"""
    if n_jobs < 0:
        n_jobs = max(cpu_count() + 1 + n_jobs, 1)

    if Y is None:
        Y = X

    ret = Parallel(n_jobs=n_jobs, verbose=0)(
        delayed(func)(X, Y[s], **kwds)
        for s in gen_even_slices(Y.shape[0], n_jobs))

    return np.hstack(ret)


def pairwise_distances(X, Y=None, metric="euclidean", n_jobs=1, **kwds):
    """ Compute the distance matrix from a vector array X and optional Y.

    This method takes either a vector array or a distance matrix, and returns
    a distance matrix. If the input is a vector array, the distances are
    computed. If the input is a distances matrix, it is returned instead.

    This method provides a safe way to take a distance matrix as input, while
    preserving compatibility with many other algorithms that take a vector
    array.

    If Y is given (default is None), then the returned matrix is the pairwise
    distance between the arrays from both X and Y.

    Please note that support for sparse matrices is currently limited to
    'euclidean', 'l2' and 'cosine'.

    Valid values for metric are:

    - from scikit-learn: ['cityblock', 'cosine', 'euclidean', 'l1', 'l2',
      'manhattan']

    - from scipy.spatial.distance: ['braycurtis', 'canberra', 'chebyshev',
      'correlation', 'dice', 'hamming', 'jaccard', 'kulsinski', 'mahalanobis',
      'matching', 'minkowski', 'rogerstanimoto', 'russellrao', 'seuclidean',
      'sokalmichener', 'sokalsneath', 'sqeuclidean', 'yule']
      See the documentation for scipy.spatial.distance for details on these
      metrics.

    Note that in the case of 'cityblock', 'cosine' and 'euclidean' (which are
    valid scipy.spatial.distance metrics), the scikit-learn implementation
    will be used, which is faster and has support for sparse matrices (except
    for 'cityblock'). For a verbose description of the metrics from
    scikit-learn, see the __doc__ of the sklearn.pairwise.distance_metrics
    function.

    Parameters
    ----------
    X : array [n_samples_a, n_samples_a] if metric == "precomputed", or, \
             [n_samples_a, n_features] otherwise
        Array of pairwise distances between samples, or a feature array.

    Y : array [n_samples_b, n_features]
        A second feature array only if X has shape [n_samples_a, n_features].

    metric : string, or callable
        The metric to use when calculating distance between instances in a
        feature array. If metric is a string, it must be one of the options
        allowed by scipy.spatial.distance.pdist for its metric parameter, or
        a metric listed in pairwise.PAIRWISE_DISTANCE_FUNCTIONS.
        If metric is "precomputed", X is assumed to be a distance matrix.
        Alternatively, if metric is a callable function, it is called on each
        pair of instances (rows) and the resulting value recorded. The callable
        should take two arrays from X as input and return a value indicating
        the distance between them.

    n_jobs : int
        The number of jobs to use for the computation. This works by breaking
        down the pairwise matrix into n_jobs even slices and computing them in
        parallel.

        If -1 all CPUs are used. If 1 is given, no parallel computing code is
        used at all, which is useful for debugging. For n_jobs below -1,
        (n_cpus + 1 + n_jobs) are used. Thus for n_jobs = -2, all CPUs but one
        are used.

    `**kwds` : optional keyword parameters
        Any further parameters are passed directly to the distance function.
        If using a scipy.spatial.distance metric, the parameters are still
        metric dependent. See the scipy docs for usage examples.

    Returns
    -------
    D : array [n_samples_a, n_samples_a] or [n_samples_a, n_samples_b]
        A distance matrix D such that D_{i, j} is the distance between the
        ith and jth vectors of the given matrix X, if Y is None.
        If Y is not None, then D_{i, j} is the distance between the ith array
        from X and the jth array from Y.

    """
    if metric == "precomputed":
        return X
    elif metric in PAIRWISE_DISTANCE_FUNCTIONS:
        func = PAIRWISE_DISTANCE_FUNCTIONS[metric]
        if n_jobs == 1:
            return func(X, Y, **kwds)
        else:
            return _parallel_pairwise(X, Y, func, n_jobs, **kwds)
    elif callable(metric):
        # Check matrices first (this is usually done by the metric).
        X, Y = check_pairwise_arrays(X, Y)
        n_x, n_y = X.shape[0], Y.shape[0]
        # Calculate distance for each element in X and Y.
        # FIXME: can use n_jobs here too
        # FIXME: np.zeros can be replaced by np.empty
        D = np.zeros((n_x, n_y), dtype='float')
        for i in range(n_x):
            start = 0
            if X is Y:
                start = i
            for j in range(start, n_y):
                # distance assumed to be symmetric.
                D[i][j] = metric(X[i], Y[j], **kwds)
                if X is Y:
                    D[j][i] = D[i][j]
        return D
    else:
        # Note: the distance module doesn't support sparse matrices!
        if type(X) is csr_matrix:
            raise TypeError("scipy distance metrics do not"
                            " support sparse matrices.")
        if Y is None:
            return distance.squareform(distance.pdist(X, metric=metric,
                                                      **kwds))
        else:
            if type(Y) is csr_matrix:
                raise TypeError("scipy distance metrics do not"
                                " support sparse matrices.")
            return distance.cdist(X, Y, metric=metric, **kwds)


# Helper functions - distance
PAIRWISE_KERNEL_FUNCTIONS = {
    # If updating this dictionary, update the doc in both distance_metrics()
    # and also in pairwise_distances()!
    'additive_chi2': additive_chi2_kernel,
    'chi2': chi2_kernel,
    'linear': linear_kernel,
    'polynomial': polynomial_kernel,
    'poly': polynomial_kernel,
    'rbf': rbf_kernel,
    'sigmoid': sigmoid_kernel,
    'cosine': cosine_similarity, }


def kernel_metrics():
    """ Valid metrics for pairwise_kernels

    This function simply returns the valid pairwise distance metrics.
    It exists, however, to allow for a verbose description of the mapping for
    each of the valid strings.

    The valid distance metrics, and the function they map to, are:
      ===============   ========================================
      metric            Function
      ===============   ========================================
      'additive_chi2'   sklearn.pairwise.additive_chi2_kernel
      'chi2'            sklearn.pairwise.chi2_kernel
      'linear'          sklearn.pairwise.linear_kernel
      'poly'            sklearn.pairwise.polynomial_kernel
      'polynomial'      sklearn.pairwise.polynomial_kernel
      'rbf'             sklearn.pairwise.rbf_kernel
      'sigmoid'         sklearn.pairwise.sigmoid_kernel
      'cosine'          sklearn.pairwise.cosine_similarity
      ===============   ========================================
    """
    return PAIRWISE_KERNEL_FUNCTIONS


KERNEL_PARAMS = {
    "additive_chi2": (),
    "chi2": (),
    "cosine": (),
    "exp_chi2": frozenset(["gamma"]),
    "linear": (),
    "poly": frozenset(["gamma", "degree", "coef0"]),
    "polynomial": frozenset(["gamma", "degree", "coef0"]),
    "rbf": frozenset(["gamma"]),
    "sigmoid": frozenset(["gamma", "coef0"]),
}


def pairwise_kernels(X, Y=None, metric="linear", filter_params=False,
                     n_jobs=1, **kwds):
    """Compute the kernel between arrays X and optional array Y.

    This method takes either a vector array or a kernel matrix, and returns
    a kernel matrix. If the input is a vector array, the kernels are
    computed. If the input is a kernel matrix, it is returned instead.

    This method provides a safe way to take a kernel matrix as input, while
    preserving compatibility with many other algorithms that take a vector
    array.

    If Y is given (default is None), then the returned matrix is the pairwise
    kernel between the arrays from both X and Y.

    Valid values for metric are::
        ['rbf', 'sigmoid', 'polynomial', 'poly', 'linear', 'cosine']

    Parameters
    ----------
    X : array [n_samples_a, n_samples_a] if metric == "precomputed", or, \
             [n_samples_a, n_features] otherwise
        Array of pairwise kernels between samples, or a feature array.

    Y : array [n_samples_b, n_features]
        A second feature array only if X has shape [n_samples_a, n_features].

    metric : string, or callable
        The metric to use when calculating kernel between instances in a
        feature array. If metric is a string, it must be one of the metrics
        in pairwise.PAIRWISE_KERNEL_FUNCTIONS.
        If metric is "precomputed", X is assumed to be a kernel matrix.
        Alternatively, if metric is a callable function, it is called on each
        pair of instances (rows) and the resulting value recorded. The callable
        should take two arrays from X as input and return a value indicating
        the distance between them.

    n_jobs : int
        The number of jobs to use for the computation. This works by breaking
        down the pairwise matrix into n_jobs even slices and computing them in
        parallel.

        If -1 all CPUs are used. If 1 is given, no parallel computing code is
        used at all, which is useful for debugging. For n_jobs below -1,
        (n_cpus + 1 + n_jobs) are used. Thus for n_jobs = -2, all CPUs but one
        are used.

    filter_params: boolean
        Whether to filter invalid parameters or not.

    `**kwds` : optional keyword parameters
        Any further parameters are passed directly to the kernel function.

    Returns
    -------
    K : array [n_samples_a, n_samples_a] or [n_samples_a, n_samples_b]
        A kernel matrix K such that K_{i, j} is the kernel between the
        ith and jth vectors of the given matrix X, if Y is None.
        If Y is not None, then K_{i, j} is the kernel between the ith array
        from X and the jth array from Y.

    Notes
    -----
    If metric is 'precomputed', Y is ignored and X is returned.

    """
    if metric == "precomputed":
        return X
    elif metric in PAIRWISE_KERNEL_FUNCTIONS:
        if filter_params:
            kwds = dict((k, kwds[k]) for k in kwds
                        if k in KERNEL_PARAMS[metric])
        func = PAIRWISE_KERNEL_FUNCTIONS[metric]
        if n_jobs == 1:
            return func(X, Y, **kwds)
        else:
            return _parallel_pairwise(X, Y, func, n_jobs, **kwds)
    elif callable(metric):
        # Check matrices first (this is usually done by the metric).
        X, Y = check_pairwise_arrays(X, Y)
        n_x, n_y = X.shape[0], Y.shape[0]
        # Calculate kernel for each element in X and Y.
        K = np.zeros((n_x, n_y), dtype='float')
        for i in range(n_x):
            start = 0
            if X is Y:
                start = i
            for j in range(start, n_y):
                # Kernel assumed to be symmetric.
                K[i][j] = metric(X[i], Y[j], **kwds)
                if X is Y:
                    K[j][i] = K[i][j]
        return K
    else:
        raise ValueError("Unknown kernel %r" % metric)
