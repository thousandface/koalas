#
# Copyright (C) 2019 Databricks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Base and utility classes for Koalas objects.
"""

from functools import wraps, partial
from typing import Union, Callable, Any

import numpy as np
import pandas as pd
from pandas.api.types import is_list_like
from pyspark import sql as spark
from pyspark.sql import functions as F, Window
from pyspark.sql.types import DoubleType, FloatType, LongType, StringType, TimestampType
from pyspark.sql.functions import monotonically_increasing_id

from databricks import koalas as ks  # For running doctests and reference resolution in PyCharm.
from databricks.koalas import numpy_compat
from databricks.koalas.internal import _InternalFrame, SPARK_INDEX_NAME_FORMAT
from databricks.koalas.typedef import pandas_wraps, spark_type_to_pandas_dtype
from databricks.koalas.utils import align_diff_series, scol_for, validate_axis
from databricks.koalas.frame import DataFrame


def booleanize_null(left_scol, scol, f):
    """
    Booleanize Null in Spark Column
    """
    comp_ops = [getattr(spark.Column, '__{}__'.format(comp_op))
                for comp_op in ['eq', 'ne', 'lt', 'le', 'ge', 'gt']]

    if f in comp_ops:
        # if `f` is "!=", fill null with True otherwise False
        filler = f == spark.Column.__ne__
        scol = F.when(scol.isNull(), filler).otherwise(scol)

    elif f == spark.Column.__or__:
        scol = F.when(left_scol.isNull() | scol.isNull(), False).otherwise(scol)

    elif f == spark.Column.__and__:
        scol = F.when(scol.isNull(), False).otherwise(scol)

    return scol


def _column_op(f):
    """
    A decorator that wraps APIs taking/returning Spark Column so that Koalas Series can be
    supported too. If this decorator is used for the `f` function that takes Spark Column and
    returns Spark Column, decorated `f` takes Koalas Series as well and returns Koalas
    Series.

    :param f: a function that takes Spark Column and returns Spark Column.
    :param self: Koalas Series
    :param args: arguments that the function `f` takes.
    """
    @wraps(f)
    def wrapper(self, *args):
        # It is possible for the function `f` takes other arguments than Spark Column.
        # To cover this case, explicitly check if the argument is Koalas Series and
        # extract Spark Column. For other arguments, they are used as are.
        cols = [arg for arg in args if isinstance(arg, IndexOpsMixin)]
        if all(self._kdf is col._kdf for col in cols):
            # Same DataFrame anchors
            args = [arg._scol if isinstance(arg, IndexOpsMixin) else arg for arg in args]
            scol = f(self._scol, *args)
            scol = booleanize_null(self._scol, scol, f)

            return self._with_new_scol(scol)
        else:
            # Different DataFrame anchors
            def apply_func(this_column, *that_columns):
                scol = f(this_column, *that_columns)
                return booleanize_null(this_column, scol, f)

            return align_diff_series(apply_func, self, *args, how="full")

    return wrapper


def _numpy_column_op(f):
    @wraps(f)
    def wrapper(self, *args):
        # PySpark does not support NumPy type out of the box. For now, we convert NumPy types
        # into some primitive types understandable in PySpark.
        new_args = []
        for arg in args:
            # TODO: This is a quick hack to support NumPy type. We should revisit this.
            if isinstance(self.spark_type, LongType) and isinstance(arg, np.timedelta64):
                new_args.append(float(arg / np.timedelta64(1, 's')))
            else:
                new_args.append(arg)
        return _column_op(f)(self, *new_args)
    return wrapper


def _wrap_accessor_spark(accessor, fn, return_type=None):
    """
    Wrap an accessor property or method, e.g., Series.dt.date with a spark function.
    """
    if return_type:
        return _column_op(
            lambda col: fn(col).cast(return_type)
        )(accessor._data)
    else:
        return _column_op(fn)(accessor._data)


def _wrap_accessor_pandas(accessor, fn, return_type):
    """
    Wrap an accessor property or method, e.g, Series.dt.date with a pandas function.
    """
    return pandas_wraps(fn, return_col=return_type)(accessor._data)


class IndexOpsMixin(object):
    """common ops mixin to support a unified interface / docs for Series / Index

    Assuming there are following attributes or properties and function.

    :ivar _scol: Spark Column instance
    :type _scol: pyspark.Column
    :ivar _kdf: Parent's Koalas DataFrame
    :type _kdf: ks.DataFrame

    :ivar spark_type: Spark data type
    :type spark_type: spark.types.DataType

    def _with_new_scol(self, scol: spark.Column) -> IndexOpsMixin
        Creates new object with the new column
    """
    def __init__(self, internal: _InternalFrame, kdf):
        assert internal is not None
        assert kdf is not None and isinstance(kdf, DataFrame)
        self._internal = internal  # type: _InternalFrame
        self._kdf = kdf

    @property
    def _scol(self):
        return self._internal.scol

    # arithmetic operators
    __neg__ = _column_op(spark.Column.__neg__)

    def __add__(self, other):
        if isinstance(self.spark_type, StringType):
            # Concatenate string columns
            if isinstance(other, IndexOpsMixin) and isinstance(other.spark_type, StringType):
                return _column_op(F.concat)(self, other)
            # Handle df['col'] + 'literal'
            elif isinstance(other, str):
                return _column_op(F.concat)(self, F.lit(other))
            else:
                raise TypeError('string addition can only be applied to string series or literals.')
        else:
            return _column_op(spark.Column.__add__)(self, other)

    def __sub__(self, other):
        # Note that timestamp subtraction casts arguments to integer. This is to mimic Pandas's
        # behaviors. Pandas returns 'timedelta64[ns]' from 'datetime64[ns]'s subtraction.
        if isinstance(other, IndexOpsMixin) and isinstance(self.spark_type, TimestampType):
            if not isinstance(other.spark_type, TimestampType):
                raise TypeError('datetime subtraction can only be applied to datetime series.')
            return self.astype('bigint') - other.astype('bigint')
        else:
            return _column_op(spark.Column.__sub__)(self, other)

    __mul__ = _column_op(spark.Column.__mul__)
    __div__ = _numpy_column_op(spark.Column.__div__)
    __truediv__ = _numpy_column_op(spark.Column.__truediv__)
    __mod__ = _column_op(spark.Column.__mod__)

    def __radd__(self, other):
        # Handle 'literal' + df['col']
        if isinstance(self.spark_type, StringType) and isinstance(other, str):
            return self._with_new_scol(F.concat(F.lit(other), self._scol))
        else:
            return _column_op(spark.Column.__radd__)(self, other)

    __rsub__ = _column_op(spark.Column.__rsub__)
    __rmul__ = _column_op(spark.Column.__rmul__)
    __rdiv__ = _numpy_column_op(spark.Column.__rdiv__)
    __rtruediv__ = _numpy_column_op(spark.Column.__rtruediv__)

    def __floordiv__(self, other):
        return self._with_new_scol(
            F.floor(_numpy_column_op(spark.Column.__div__)(self, other)._scol))

    def __rfloordiv__(self, other):
        return self._with_new_scol(
            F.floor(_numpy_column_op(spark.Column.__rdiv__)(self, other)._scol))

    __rmod__ = _column_op(spark.Column.__rmod__)
    __pow__ = _column_op(spark.Column.__pow__)
    __rpow__ = _column_op(spark.Column.__rpow__)

    # comparison operators
    __eq__ = _column_op(spark.Column.__eq__)
    __ne__ = _column_op(spark.Column.__ne__)
    __lt__ = _column_op(spark.Column.__lt__)
    __le__ = _column_op(spark.Column.__le__)
    __ge__ = _column_op(spark.Column.__ge__)
    __gt__ = _column_op(spark.Column.__gt__)

    # `and`, `or`, `not` cannot be overloaded in Python,
    # so use bitwise operators as boolean operators
    __and__ = _column_op(spark.Column.__and__)
    __or__ = _column_op(spark.Column.__or__)
    __invert__ = _column_op(spark.Column.__invert__)
    __rand__ = _column_op(spark.Column.__rand__)
    __ror__ = _column_op(spark.Column.__ror__)

    # NDArray Compat
    def __array_ufunc__(self, ufunc: Callable, method: str, *inputs: Any, **kwargs: Any):
        # Try dunder methods first.
        result = numpy_compat.maybe_dispatch_ufunc_to_dunder_op(
            self, ufunc, method, *inputs, **kwargs)

        # After that, we try with PySpark APIs.
        if result is NotImplemented:
            result = numpy_compat.maybe_dispatch_ufunc_to_spark_func(
                self, ufunc, method, *inputs, **kwargs)

        if result is not NotImplemented:
            return result
        else:
            # TODO: support more APIs?
            raise NotImplementedError("Koalas objects currently do not support %s." % ufunc)

    @property
    def dtype(self):
        """Return the dtype object of the underlying data.

        Examples
        --------
        >>> s = ks.Series([1, 2, 3])
        >>> s.dtype
        dtype('int64')

        >>> s = ks.Series(list('abc'))
        >>> s.dtype
        dtype('O')

        >>> s = ks.Series(pd.date_range('20130101', periods=3))
        >>> s.dtype
        dtype('<M8[ns]')

        >>> s.rename("a").to_frame().set_index("a").index.dtype
        dtype('<M8[ns]')
        """
        return spark_type_to_pandas_dtype(self.spark_type)

    @property
    def empty(self):
        """
        Returns true if the current object is empty. Otherwise, returns false.

        >>> ks.range(10).id.empty
        False

        >>> ks.range(0).id.empty
        True

        >>> ks.DataFrame({}, index=list('abc')).index.empty
        False
        """
        return self._internal._sdf.rdd.isEmpty()

    @property
    def hasnans(self):
        """
        Return True if it has any missing values. Otherwise, it returns False.

        >>> ks.DataFrame({}, index=list('abc')).index.hasnans
        False

        >>> ks.Series(['a', None]).hasnans
        True

        >>> ks.Series([1.0, 2.0, np.nan]).hasnans
        True

        >>> ks.Series([1, 2, 3]).hasnans
        False

        >>> ks.Series([1, 2, 3]).rename("a").to_frame().set_index("a").index.hasnans
        False
        """
        sdf = self._internal._sdf.select(self._scol)
        col = self._scol

        ret = sdf.select(F.max(col.isNull() | F.isnan(col))).collect()[0][0]
        return ret

    @property
    def is_monotonic(self):
        """
        Return boolean if values in the object are monotonically increasing.

        .. note:: the current implementation of is_monotonic_increasing uses Spark's
            Window without specifying partition specification. This leads to move all data into
            single partition in single machine and could cause serious
            performance degradation. Avoid this method against very large dataset.

        Returns
        -------
        is_monotonic : boolean

        Examples
        --------
        >>> ser = ks.Series(['1/1/2018', '3/1/2018', '4/1/2018'])
        >>> ser.is_monotonic
        True

        >>> df = ks.DataFrame({'dates': [None, '1/1/2018', '2/1/2018', '3/1/2018']})
        >>> df.dates.is_monotonic
        False

        >>> df.index.is_monotonic
        True

        >>> ser = ks.Series([1])
        >>> ser.is_monotonic
        True

        >>> ser = ks.Series([])
        >>> ser.is_monotonic
        True

        >>> ser.rename("a").to_frame().set_index("a").index.is_monotonic
        True

        >>> ser = ks.Series([5, 4, 3, 2, 1], index=[1, 2, 3, 4, 5])
        >>> ser.is_monotonic
        False

        >>> ser.index.is_monotonic
        True
        """
        col = self._scol
        window = Window.orderBy(monotonically_increasing_id()).rowsBetween(-1, -1)
        return self._with_new_scol((col >= F.lag(col, 1).over(window)) & col.isNotNull()).all()

    is_monotonic_increasing = is_monotonic

    @property
    def is_monotonic_decreasing(self):
        """
        Return boolean if values in the object are monotonically decreasing.

        .. note:: the current implementation of is_monotonic_decreasing uses Spark's
            Window without specifying partition specification. This leads to move all data into
            single partition in single machine and could cause serious
            performance degradation. Avoid this method against very large dataset.

        Returns
        -------
        is_monotonic : boolean

        Examples
        --------
        >>> ser = ks.Series(['4/1/2018', '3/1/2018', '1/1/2018'])
        >>> ser.is_monotonic_decreasing
        True

        >>> df = ks.DataFrame({'dates': [None, '3/1/2018', '2/1/2018', '1/1/2018']})
        >>> df.dates.is_monotonic_decreasing
        False

        >>> df.index.is_monotonic_decreasing
        False

        >>> ser = ks.Series([1])
        >>> ser.is_monotonic_decreasing
        True

        >>> ser = ks.Series([])
        >>> ser.is_monotonic_decreasing
        True

        >>> ser.rename("a").to_frame().set_index("a").index.is_monotonic_decreasing
        True

        >>> ser = ks.Series([5, 4, 3, 2, 1], index=[1, 2, 3, 4, 5])
        >>> ser.is_monotonic_decreasing
        True

        >>> ser.index.is_monotonic_decreasing
        False
        """
        col = self._scol
        window = Window.orderBy(monotonically_increasing_id()).rowsBetween(-1, -1)
        return self._with_new_scol((col <= F.lag(col, 1).over(window)) & col.isNotNull()).all()

    @property
    def ndim(self):
        """
        Return an int representing the number of array dimensions.

        Return 1 for Series / Index / MultiIndex.

        Examples
        --------

        For Series

        >>> s = ks.Series([None, 1, 2, 3, 4], index=[4, 5, 2, 1, 8])
        >>> s.ndim
        1

        For Index

        >>> s.index.ndim
        1

        For MultiIndex

        >>> midx = pd.MultiIndex([['lama', 'cow', 'falcon'],
        ...                       ['speed', 'weight', 'length']],
        ...                      [[0, 0, 0, 1, 1, 1, 2, 2, 2],
        ...                       [1, 1, 1, 1, 1, 2, 1, 2, 2]])
        >>> s = ks.Series([45, 200, 1.2, 30, 250, 1.5, 320, 1, 0.3], index=midx)
        >>> s.index.ndim
        1
        """
        return 1

    def astype(self, dtype):
        """
        Cast a Koalas object to a specified dtype ``dtype``.

        Parameters
        ----------
        dtype : data type
            Use a numpy.dtype or Python type to cast entire pandas object to
            the same type.

        Returns
        -------
        casted : same type as caller

        See Also
        --------
        to_datetime : Convert argument to datetime.

        Examples
        --------
        >>> ser = ks.Series([1, 2], dtype='int32')
        >>> ser
        0    1
        1    2
        Name: 0, dtype: int32

        >>> ser.astype('int64')
        0    1
        1    2
        Name: 0, dtype: int64

        >>> ser.rename("a").to_frame().set_index("a").index.astype('int64')
        Int64Index([1, 2], dtype='int64', name='a')
        """
        from databricks.koalas.typedef import as_spark_type
        spark_type = as_spark_type(dtype)
        if not spark_type:
            raise ValueError("Type {} not understood".format(dtype))
        return self._with_new_scol(self._scol.cast(spark_type))

    def isin(self, values):
        """
        Check whether `values` are contained in Series.

        Return a boolean Series showing whether each element in the Series
        matches an element in the passed sequence of `values` exactly.

        Parameters
        ----------
        values : list or set
            The sequence of values to test.

        Returns
        -------
        isin : Series (bool dtype)

        Examples
        --------
        >>> s = ks.Series(['lama', 'cow', 'lama', 'beetle', 'lama',
        ...                'hippo'], name='animal')
        >>> s.isin(['cow', 'lama'])
        0     True
        1     True
        2     True
        3    False
        4     True
        5    False
        Name: animal, dtype: bool

        Passing a single string as ``s.isin('lama')`` will raise an error. Use
        a list of one element instead:

        >>> s.isin(['lama'])
        0     True
        1    False
        2     True
        3    False
        4     True
        5    False
        Name: animal, dtype: bool

        >>> s.rename("a").to_frame().set_index("a").index.isin(['lama'])
        Index([True, False, True, False, True, False], dtype='object', name='a')
        """
        if not is_list_like(values):
            raise TypeError("only list-like objects are allowed to be passed"
                            " to isin(), you passed a [{values_type}]"
                            .format(values_type=type(values).__name__))

        return self._with_new_scol(self._scol.isin(list(values))).rename(self.name)

    def isnull(self):
        """
        Detect existing (non-missing) values.

        Return a boolean same-sized object indicating if the values are NA.
        NA values, such as None or numpy.NaN, gets mapped to True values.
        Everything else gets mapped to False values. Characters such as empty strings '' or
        numpy.inf are not considered NA values
        (unless you set pandas.options.mode.use_inf_as_na = True).

        Returns
        -------
        Series : Mask of bool values for each element in Series
            that indicates whether an element is not an NA value.

        Examples
        --------
        >>> ser = ks.Series([5, 6, np.NaN])
        >>> ser.isna()  # doctest: +NORMALIZE_WHITESPACE
        0    False
        1    False
        2     True
        Name: 0, dtype: bool

        >>> ser.rename("a").to_frame().set_index("a").index.isna()
        Index([False, False, True], dtype='object', name='a')
        """
        from databricks.koalas.indexes import MultiIndex
        if isinstance(self, MultiIndex):
            raise NotImplementedError("isna is not defined for MultiIndex")
        if isinstance(self.spark_type, (FloatType, DoubleType)):
            return self._with_new_scol(self._scol.isNull() | F.isnan(self._scol)).rename(self.name)
        else:
            return self._with_new_scol(self._scol.isNull()).rename(self.name)

    isna = isnull

    def notnull(self):
        """
        Detect existing (non-missing) values.
        Return a boolean same-sized object indicating if the values are not NA.
        Non-missing values get mapped to True.
        Characters such as empty strings '' or numpy.inf are not considered NA values
        (unless you set pandas.options.mode.use_inf_as_na = True).
        NA values, such as None or numpy.NaN, get mapped to False values.

        Returns
        -------
        Series : Mask of bool values for each element in Series
            that indicates whether an element is not an NA value.

        Examples
        --------
        Show which entries in a Series are not NA.

        >>> ser = ks.Series([5, 6, np.NaN])
        >>> ser
        0    5.0
        1    6.0
        2    NaN
        Name: 0, dtype: float64

        >>> ser.notna()
        0     True
        1     True
        2    False
        Name: 0, dtype: bool

        >>> ser.rename("a").to_frame().set_index("a").index.notna()
        Index([True, True, False], dtype='object', name='a')
        """
        from databricks.koalas.indexes import MultiIndex
        if isinstance(self, MultiIndex):
            raise NotImplementedError("notna is not defined for MultiIndex")
        return (~self.isnull()).rename(self.name)

    notna = notnull

    # TODO: axis, skipna, and many arguments should be implemented.
    def all(self, axis: Union[int, str] = 0) -> bool:
        """
        Return whether all elements are True.

        Returns True unless there at least one element within a series that is
        False or equivalent (e.g. zero or empty)

        Parameters
        ----------
        axis : {0 or 'index'}, default 0
            Indicate which axis or axes should be reduced.

            * 0 / 'index' : reduce the index, return a Series whose index is the
              original column labels.

        Examples
        --------
        >>> ks.Series([True, True]).all()
        True

        >>> ks.Series([True, False]).all()
        False

        >>> ks.Series([0, 1]).all()
        False

        >>> ks.Series([1, 2, 3]).all()
        True

        >>> ks.Series([True, True, None]).all()
        True

        >>> ks.Series([True, False, None]).all()
        False

        >>> ks.Series([]).all()
        True

        >>> ks.Series([np.nan]).all()
        True

        >>> df = ks.Series([True, False, None]).rename("a").to_frame()
        >>> df.set_index("a").index.all()
        False
        """
        axis = validate_axis(axis)
        if axis != 0:
            raise ValueError('axis should be either 0 or "index" currently.')

        sdf = self._internal._sdf.select(self._scol)
        col = scol_for(sdf, sdf.columns[0])

        # Note that we're ignoring `None`s here for now.
        # any and every was added as of Spark 3.0
        # ret = sdf.select(F.expr("every(CAST(`%s` AS BOOLEAN))" % sdf.columns[0])).collect()[0][0]
        # Here we use min as its alternative:
        ret = sdf.select(F.min(F.coalesce(col.cast('boolean'), F.lit(True)))).collect()[0][0]
        if ret is None:
            return True
        else:
            return ret

    # TODO: axis, skipna, and many arguments should be implemented.
    def any(self, axis: Union[int, str] = 0) -> bool:
        """
        Return whether any element is True.

        Returns False unless there at least one element within a series that is
        True or equivalent (e.g. non-zero or non-empty).

        Parameters
        ----------
        axis : {0 or 'index'}, default 0
            Indicate which axis or axes should be reduced.

            * 0 / 'index' : reduce the index, return a Series whose index is the
              original column labels.

        Examples
        --------
        >>> ks.Series([False, False]).any()
        False

        >>> ks.Series([True, False]).any()
        True

        >>> ks.Series([0, 0]).any()
        False

        >>> ks.Series([0, 1, 2]).any()
        True

        >>> ks.Series([False, False, None]).any()
        False

        >>> ks.Series([True, False, None]).any()
        True

        >>> ks.Series([]).any()
        False

        >>> ks.Series([np.nan]).any()
        False

        >>> df = ks.Series([True, False, None]).rename("a").to_frame()
        >>> df.set_index("a").index.any()
        True
        """
        axis = validate_axis(axis)
        if axis != 0:
            raise ValueError('axis should be either 0 or "index" currently.')

        sdf = self._internal._sdf.select(self._scol)
        col = scol_for(sdf, sdf.columns[0])

        # Note that we're ignoring `None`s here for now.
        # any and every was added as of Spark 3.0
        # ret = sdf.select(F.expr("any(CAST(`%s` AS BOOLEAN))" % sdf.columns[0])).collect()[0][0]
        # Here we use max as its alternative:
        ret = sdf.select(F.max(F.coalesce(col.cast('boolean'), F.lit(False)))).collect()[0][0]
        if ret is None:
            return False
        else:
            return ret

    # TODO: add frep and axis parameter
    def shift(self, periods=1, fill_value=None):
        """
        Shift Series/Index by desired number of periods.

        .. note:: the current implementation of shift uses Spark's Window without
            specifying partition specification. This leads to move all data into
            single partition in single machine and could cause serious
            performance degradation. Avoid this method against very large dataset.

        Parameters
        ----------
        periods : int
            Number of periods to shift. Can be positive or negative.
        fill_value : object, optional
            The scalar value to use for newly introduced missing values.
            The default depends on the dtype of self. For numeric data, np.nan is used.

        Returns
        -------
        Copy of input Series/Index, shifted.

        Examples
        --------
        >>> df = ks.DataFrame({'Col1': [10, 20, 15, 30, 45],
        ...                    'Col2': [13, 23, 18, 33, 48],
        ...                    'Col3': [17, 27, 22, 37, 52]},
        ...                   columns=['Col1', 'Col2', 'Col3'])

        >>> df.Col1.shift(periods=3)
        0     NaN
        1     NaN
        2     NaN
        3    10.0
        4    20.0
        Name: Col1, dtype: float64

        >>> df.Col2.shift(periods=3, fill_value=0)
        0     0
        1     0
        2     0
        3    13
        4    23
        Name: Col2, dtype: int64

        >>> df.index.shift(periods=3, fill_value=0)
        Int64Index([0, 0, 0, 0, 1], dtype='int64')
        """
        return self._shift(periods, fill_value)

    def _shift(self, periods, fill_value, part_cols=()):
        if not isinstance(periods, int):
            raise ValueError('periods should be an int; however, got [%s]' % type(periods))

        col = self._scol
        window = Window.partitionBy(*part_cols).orderBy(self._internal.index_scols)\
            .rowsBetween(-periods, -periods)
        lag_col = F.lag(col, periods).over(window)
        col = F.when(lag_col.isNull() | F.isnan(lag_col), fill_value).otherwise(lag_col)
        return self._with_new_scol(col).rename(self.name)

    # TODO: Update Documentation for Bins Parameter when its supported
    def value_counts(self, normalize=False, sort=True, ascending=False, bins=None, dropna=True):
        """
        Return a Series containing counts of unique values.
        The resulting object will be in descending order so that the
        first element is the most frequently-occurring element.
        Excludes NA values by default.

        Parameters
        ----------
        normalize : boolean, default False
            If True then the object returned will contain the relative
            frequencies of the unique values.
        sort : boolean, default True
            Sort by values.
        ascending : boolean, default False
            Sort in ascending order.
        bins : Not Yet Supported
        dropna : boolean, default True
            Don't include counts of NaN.

        Returns
        -------
        counts : Series

        See Also
        --------
        Series.count: Number of non-NA elements in a Series.

        Examples
        --------
        For Series

        >>> df = ks.DataFrame({'x':[0, 0, 1, 1, 1, np.nan]})
        >>> df.x.value_counts()  # doctest: +NORMALIZE_WHITESPACE
        1.0    3
        0.0    2
        Name: x, dtype: int64

        With `normalize` set to `True`, returns the relative frequency by
        dividing all values by the sum of values.

        >>> df.x.value_counts(normalize=True)  # doctest: +NORMALIZE_WHITESPACE
        1.0    0.6
        0.0    0.4
        Name: x, dtype: float64

        **dropna**
        With `dropna` set to `False` we can also see NaN index values.

        >>> df.x.value_counts(dropna=False)  # doctest: +NORMALIZE_WHITESPACE
        1.0    3
        0.0    2
        NaN    1
        Name: x, dtype: int64

        For Index

        >>> from databricks.koalas.indexes import Index
        >>> idx = Index([3, 1, 2, 3, 4, np.nan])
        >>> idx
        Float64Index([3.0, 1.0, 2.0, 3.0, 4.0, nan], dtype='float64')

        >>> idx.value_counts().sort_index()
        1.0    1
        2.0    1
        3.0    2
        4.0    1
        Name: count, dtype: int64

        **sort**

        With `sort` set to `False`, the result wouldn't be sorted by number of count.

        >>> idx.value_counts(sort=True).sort_index()
        1.0    1
        2.0    1
        3.0    2
        4.0    1
        Name: count, dtype: int64

        **normalize**

        With `normalize` set to `True`, returns the relative frequency by
        dividing all values by the sum of values.

        >>> idx.value_counts(normalize=True).sort_index()
        1.0    0.2
        2.0    0.2
        3.0    0.4
        4.0    0.2
        Name: count, dtype: float64

        **dropna**

        With `dropna` set to `False` we can also see NaN index values.

        >>> idx.value_counts(dropna=False).sort_index()  # doctest: +SKIP
        1.0    1
        2.0    1
        3.0    2
        4.0    1
        NaN    1
        Name: count, dtype: int64

        For MultiIndex.

        >>> midx = pd.MultiIndex([['lama', 'cow', 'falcon'],
        ...                       ['speed', 'weight', 'length']],
        ...                      [[0, 0, 0, 1, 1, 1, 2, 2, 2],
        ...                       [1, 1, 1, 1, 1, 2, 1, 2, 2]])
        >>> s = ks.Series([45, 200, 1.2, 30, 250, 1.5, 320, 1, 0.3], index=midx)
        >>> s.index  # doctest: +SKIP
        MultiIndex([(  'lama', 'weight'),
                    (  'lama', 'weight'),
                    (  'lama', 'weight'),
                    (   'cow', 'weight'),
                    (   'cow', 'weight'),
                    (   'cow', 'length'),
                    ('falcon', 'weight'),
                    ('falcon', 'length'),
                    ('falcon', 'length')],
                   )

        >>> s.index.value_counts().sort_index()
        (cow, length)       1
        (cow, weight)       2
        (falcon, length)    2
        (falcon, weight)    1
        (lama, weight)      3
        Name: count, dtype: int64

        >>> s.index.value_counts(normalize=True).sort_index()
        (cow, length)       0.111111
        (cow, weight)       0.222222
        (falcon, length)    0.222222
        (falcon, weight)    0.111111
        (lama, weight)      0.333333
        Name: count, dtype: float64

        If Index has name, keep the name up.

        >>> idx = Index([0, 0, 0, 1, 1, 2, 3], name='koalas')
        >>> idx.value_counts().sort_index()
        0    3
        1    2
        2    1
        3    1
        Name: koalas, dtype: int64
        """
        from databricks.koalas.series import _col

        if bins is not None:
            raise NotImplementedError("value_counts currently does not support bins")

        if dropna:
            sdf_dropna = self._internal._sdf.select(self._scol).dropna()
        else:
            sdf_dropna = self._internal._sdf.select(self._scol)
        index_name = SPARK_INDEX_NAME_FORMAT(0)
        column_name = self._internal.data_columns[0]
        sdf = sdf_dropna.groupby(scol_for(sdf_dropna, column_name).alias(index_name)).count()
        if sort:
            if ascending:
                sdf = sdf.orderBy(F.col('count'))
            else:
                sdf = sdf.orderBy(F.col('count').desc())

        if normalize:
            sum = sdf_dropna.count()
            sdf = sdf.withColumn('count', F.col('count') / F.lit(sum))

        column_index = self._internal.column_index
        if (column_index[0] is None) or (None in column_index[0]):
            internal = _InternalFrame(sdf=sdf,
                                      index_map=[(index_name, None)],
                                      column_scols=[scol_for(sdf, 'count')])
        else:
            internal = _InternalFrame(sdf=sdf,
                                      index_map=[(index_name, None)],
                                      column_index=column_index,
                                      column_scols=[scol_for(sdf, 'count')],
                                      column_index_names=self._internal.column_index_names)

        return _col(DataFrame(internal))

    def nunique(self, dropna: bool = True, approx: bool = False, rsd: float = 0.05) -> int:
        """
        Return number of unique elements in the object.
        Excludes NA values by default.

        Parameters
        ----------
        dropna : bool, default True
            Don’t include NaN in the count.
        approx: bool, default False
            If False, will use the exact algorithm and return the exact number of unique.
            If True, it uses the HyperLogLog approximate algorithm, which is significantly faster
            for large amount of data.
            Note: This parameter is specific to Koalas and is not found in pandas.
        rsd: float, default 0.05
            Maximum estimation error allowed in the HyperLogLog algorithm.
            Note: Just like ``approx`` this parameter is specific to Koalas.

        Returns
        -------
        int

        See Also
        --------
        DataFrame.nunique: Method nunique for DataFrame.
        Series.count: Count non-NA/null observations in the Series.

        Examples
        --------
        >>> ks.Series([1, 2, 3, np.nan]).nunique()
        3

        >>> ks.Series([1, 2, 3, np.nan]).nunique(dropna=False)
        4

        On big data, we recommend using the approximate algorithm to speed up this function.
        The result will be very close to the exact unique count.

        >>> ks.Series([1, 2, 3, np.nan]).nunique(approx=True)
        3

        >>> idx = ks.Index([1, 1, 2, None])
        >>> idx
        Float64Index([1.0, 1.0, 2.0, nan], dtype='float64')

        >>> idx.nunique()
        2

        >>> idx.nunique(dropna=False)
        3
        """
        res = self._internal._sdf.select([self._nunique(dropna, approx, rsd)])
        return res.collect()[0][0]

    def _nunique(self, dropna=True, approx=False, rsd=0.05):
        colname = self._internal.data_columns[0]
        count_fn = partial(F.approx_count_distinct, rsd=rsd) if approx else F.countDistinct
        if dropna:
            return count_fn(self._scol).alias(colname)
        else:
            return (count_fn(self._scol) +
                    F.when(F.count(F.when(self._scol.isNull(), 1)
                                   .otherwise(None)) >= 1, 1).otherwise(0)).alias(colname)
