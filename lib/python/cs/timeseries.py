#!/usr/bin/env python3

''' Efficient portable machine native columnar storage of time series data
    for double float and signed 64-bit integers.

    I use this as efficient storage of time series data from my solar inverter,
    which reports in a slightly clunky time limited CSV format;
    I import those CSVs into `TimeSeries` data directories
    which contain the overall accrued data.

    The `TimeSeries` and related classes provide methods
    for providing the data as `pandas.Series` instances etc.
'''

from abc import ABC, abstractmethod
from array import array, typecodes  # pylint: disable=no-name-in-module
from collections import defaultdict
from contextlib import contextmanager
from functools import partial
from getopt import GetoptError
import os
from os.path import (
    dirname,
    isdir as isdirpath,
    isfile as isfilepath,
    join as joinpath,
    normpath,
)
from struct import pack, Struct  # pylint: disable=no-name-in-module
import sys
import time
from typing import Optional, Tuple, Union

import arrow
from arrow import Arrow
from icontract import ensure, require, DBC
from numpy import datetime64
from pandas import Series as PDSeries
from typeguard import typechecked

from cs.cmdutils import BaseCommand
from cs.deco import cachedmethod, decorator
from cs.fs import HasFSPath, fnmatchdir, is_clean_subpath, shortpath
from cs.fstags import FSTags
from cs.logutils import warning
from cs.pfx import pfx, pfx_call, Pfx
from cs.py.modules import import_extra
from cs.resources import MultiOpenMixin
from cs.tagset import TagSet

from cs.x import X

DISTINFO = {
    'keywords': ["python3"],
    'classifiers': [
        "Development Status :: 3 - Alpha",
        "Programming Language :: Python :: 3",
    ],
    'install_requires': [],
    'entry_points': {
        'console_scripts': [
            'csts = cs.timeseries:main',
        ],
    },
    'extras_requires': {
        'numpy': ['numpy'],
        'pandas': ['pandas'],
        'plotting': ['kaleido', 'plotly'],
    },
}

Numeric = Union[int, float]

def main(argv=None):
  ''' Run the command line tool for `TimeSeries` data.
  '''
  return TimeSeriesCommand(argv).run()

pfx_listdir = partial(pfx_call, os.listdir)
pfx_mkdir = partial(pfx_call, os.mkdir)
pfx_open = partial(pfx_call, open)

# initial support is singled 64 bit integers and double floats
SUPPORTED_TYPECODES = {
    'q': int,
    'd': float,
}
assert all(typecode in typecodes for typecode in SUPPORTED_TYPECODES)

@typechecked
@require(lambda typecode: typecode in SUPPORTED_TYPECODES)
def deduce_type_bigendianness(typecode: str) -> bool:
  ''' Deduce the native endianness for `typecode`,
      an array/struct typecode character.
  '''
  test_value = SUPPORTED_TYPECODES[typecode](1)
  bs_a = array(typecode, (test_value,)).tobytes()
  bs_s_be = pack('>' + typecode, test_value)
  bs_s_le = pack('<' + typecode, test_value)
  if bs_a == bs_s_be:
    return True
  if bs_a == bs_s_le:
    return False
  raise RuntimeError(
      "cannot infer byte order: array(%r,(1,))=%r, pack(>%s,1)=%r, pack(<%s,1)=%r"
      % (typecode, bs_a, typecode, bs_s_be, typecode, bs_s_le)
  )

NATIVE_BIGENDIANNESS = {
    typecode: deduce_type_bigendianness(typecode)
    for typecode in SUPPORTED_TYPECODES
}

class TimeSeriesCommand(BaseCommand):
  ''' Command line interface to `TimeSeries` data files.
  '''

  SUBCOMMAND_ARGV_DEFAULT = 'test'

  def cmd_test(self, argv):
    ''' Usage: {cmd} [testnames...]
          Run some tests of functionality.
    '''
    if not argv:
      argv = ['pandas']

    def test_pandas():
      t0 = 1649552238
      fspath = f'foo--from-{t0}.dat'
      ts = TimeSeries(fspath, 'd', start=t0, step=1)
      ary = ts.array
      ts.pad_to(time.time() + 300)
      print("len(ts) =", len(ts))
      pds = ts.as_pd_series()
      print(type(pds), pds.memory_usage())
      print(pds)

    def test_tagged_spans():
      policy = TimespanPolicyDaily()
      start = time.time()
      end = time.time() + 7 * 24 * 3600
      print("start =", Arrow.fromtimestamp(start))
      print("end =", Arrow.fromtimestamp(end))
      for tag, tag_start, tag_end in policy.tagged_spans(start, end):
        print(
            tag, Arrow.fromtimestamp(tag_start), Arrow.fromtimestamp(tag_end)
        )

    def test_datadir():
      with TimeSeriesDataDir('tsdatadir', policy='daily', step=300) as datadir:
        ts = datadir['key1']
        ts[time.time()] = 9.0

    def test_timespan_policy():
      policy = TimespanPolicyMonthly()
      policy.timespan_for(time.time())

    def test_timeseries():
      t0 = 1649464235
      fspath = 'foo.dat'
      ts = TimeSeries(fspath, 'd', start=t0, step=1)
      ary = ts.array
      print(ary)
      ts.pad_to(time.time() + 300)
      print(ary)
      ts.save()

    testfunc_map = {
        'datadir': test_datadir,
        'pandas': test_pandas,
        'tagged_spans': test_tagged_spans,
        'timeseries': test_timeseries,
        'timespan_policy': test_timespan_policy,
    }
    ok = True
    for testname in argv:
      with Pfx(testname):
        if testname not in testfunc_map:
          warning("unknown test name")
          ok = False
    if not ok:
      raise GetoptError(
          "unknown test names, I know: %s" %
          (", ".join(sorted(testfunc_map.keys())),)
      )
    for testname in argv:
      with Pfx(testname):
        testfunc_map[testname]()

@decorator
def plotrange(func, needs_start=False, needs_stop=False):
  ''' A decorator for plotting methods with optional `start` and `stop`
      leading positional parameters and an optional `figure` keyword parameter.

      The decorator parameters `needs_start` and `needs_stop`
      may be set to require non-`None` values for `start` and `stop`.

      If `start` is `None` its value is set to `self.start`.
      If `stop` is `None` its value is set to `self.stop`.
      If `figure` is `None` its value is set to a new
      `plotly.graph_objects.Figure` instance.

      The decorated method is then called as:

          func(self, start, stop, *a, figure=figure, **kw)

      where `*a` and `**kw` are the additional positional and keyword
      parameters respectively, if any.
  '''

  @require(lambda start: not needs_start or start is not None)
  @require(lambda stop: not needs_stop or stop is not None)
  def plotrange_wrapper(self, start=None, stop=None, *a, figure=None, **kw):
    plotly = import_extra('plotly', DISTINFO)
    go = plotly.graph_objects
    if start is None:
      start = self.start
    if stop is None:
      stop = self.stop
    if figure is None:
      figure = go.Figure()
    return func(self, start, stop, *a, figure=figure, **kw)

  return plotrange_wrapper

def get_default_timezone_name():
  ''' Return the default timezone name.
  '''
  return arrow.now('local').format('ZZZ')

@require(lambda typecode: typecode in SUPPORTED_TYPECODES)
def struct_format(typecode, bigendian):
  ''' Return a `struct` format string for the supplied `typecode` and big endianness.
  '''
  return ('>' if bigendian else '<') + typecode

@contextmanager
def array_byteswapped(ary):
  ''' Context manager to byteswap the `array.array` `ary` temporarily.
  '''
  ary.byteswap()
  try:
    yield
  finally:
    ary.byteswap()

class TimeStepsMixin:
  ''' Methods for an object with `start` and `step` attributes.
  '''

  def offset(self, when) -> int:
    ''' Return the step offset for the UNIX time `when` from `self.start`.

        Eample in a `TimeSeries`:

           >>> ts = TimeSeries('tsfile.csts', 'd', start=19.1, step=1.2)
           >>> ts.offset(19.1)
           0
           >>> ts.offset(20)
           0
           >>> ts.offset(22)
           2
    '''
    offset = when - self.start
    offset_steps = offset // self.step
    when0 = self.start + offset_steps * self.step
    if when0 < self.start:
      offset_steps += 1
    offset_steps_i = int(offset_steps)
    assert offset_steps == offset_steps_i
    return offset_steps_i

  def when(self, offset):
    ''' Return `self.start+offset*self.step`.
    '''
    return self.start + offset * self.step

  def offset_bounds(self, start, stop) -> (int, int):
    offset_steps = self.offset(start)
    end_offset_steps = self.offset(stop)
    if end_offset_steps == offset_steps and stop > start:
      end_offset_steps += 1
    return offset_steps, end_offset_steps

  def offset_range(self, start, stop):
    ''' Return an iterable of the offsets from `start` to `stop`
        in units of `self.step`
        i.e. `offset(start) == 0`.

        Eample in a `TimeSeries`:

           >>> ts = TimeSeries('tsfile.csts', 'd', start=19.1, step=1.2)
           >>> list(ts.offset_range(20,30))
           [0, 1, 2, 3, 4, 5, 6, 7, 8]
    '''
    if start < self.start:
      raise IndexError(
          "start:%s must be >= self.start:%s" % (start, self.start)
      )
    if stop < start:
      raise IndexError("start:%s must be <= stop:%s" % (start, stop))
    offset_steps, end_offset_steps = self.offset_bounds(start, stop)
    return range(offset_steps, end_offset_steps)

  def round_down(self, when):
    ''' Return `when` rounded down to the start of its time slot.
    '''
    return self.when(self.offset(when))

  def round_up(self, when, start, step):
    ''' Return `when` rounded up to the next time slot.
    '''
    rounded = self.round_down(when)
    if rounded < when:
      rounded = self.when(self.offset(when) + 1)
    return rounded

  def range(self, start, stop):
    ''' Return an iterable of the times from `start` to `stop`.

        Eample in a `TimeSeries`:

           >>> ts = TimeSeries('tsfile.csts', 'd', start=19.1, step=1.2)
           >>> list(ts.range(20,30))
           [19.1, 20.3, 21.5, 22.700000000000003, 23.900000000000002, 25.1, 26.3, 27.5, 28.700000000000003]


        Note that if the `TimeSeries` uses `float` values for `start` and `step`
        then the values returned here will not necessarily round trip
        to array indicies because of rounding.

        As such, these times are useful for supplying the index to
        a time series as might be wanted for a graph, but are not
        reliably useful to _obtain_ the values from the time series.
        So this is reliable:

            # works well: pair up values with their times
            graph_data = zip(ts.range(20,30), ts[20:30])

        but this is unreliable because of rounding:

            # unreliable: pair up values with their times
            times = list(ts.range(20, 30))
            graph_data = zip(times, [ts[t] for t in times])

        The reliable form is available as the `data(start,stop)` method.

        Instead, the reliable way to obtain the values between the
        UNIX times `start` and `stop` is to directly fetch them
        from the `array` underlying the `TimeSeries`.
        This can be done using the `offset_bounds`
        or `array_indices` methods to obtain the `array` indices,
        for example:

            astart, astop = ts.offset_bounds(start, stop)
            return ts.array[astart:astop]

        or more conveniently by slicing the `TimeSeries`:

            values = ts[start:stop]
    '''
    # this would be a range but they only work in integers
    return (
        self.start + self.step * offset_step
        for offset_step in self.offset_range(start, stop)
    )

class TimeSeries(MultiOpenMixin, TimeStepsMixin):
  ''' A single time series for a single data field.

      This provides easy access to a time series data file.
      The instance can be indexed by UNIX time stamp for time based access
      or its `.array` property can be accessed for the raw data.

      Read only users can just instantiate an instance.
      Read/write users should use the instance as a context manager,
      which will automatically rewrite the file with the array data
      on exit.

      Note that the save-on-close is done with `TimeSeries.flush()`
      which ony saves if `self.modified`.
      Use of the `__setitem__` or `pad_to` methods set this flag automatically.
      Direct access via the `.array` will not set it,
      so users working that way for performance should update the flag themselves.

      The data file itself has a header indicating the file data big endianness
      and datum type (an `array.array` type code).
      This is automatically honoured on load and save.
      Note that the header _does not_ indicate the `start`,`step` time range of the data.
  '''

  DOTEXT = '.csts'
  MAGIC = b'csts'
  HEADER_LENGTH = 8

  @typechecked
  def __init__(
      self,
      fspath: str,
      typecode: Optional[str] = None,
      *,
      start: Union[int, float],
      step: Union[int, float],
      fill=None,
  ):
    ''' Prepare a new time series stored in the file at `fspath`
        containing machine data for the time series values.

        Parameters:
        * `fspath`: the filename of the data file
        * `typecode` optional expected `array.typecode` value of the data;
          if specified and the data file exists, they must match;
          if not specified then the data file must exist
          and the `typecode` will be obtained from its header
        * `start`: the UNIX epoch time for the first datum
        * `step`: the increment between data times
        * `fill`: optional default fill values for `pad_to`;
          if unspecified, fill with `0` for `'q'`
          and `float('nan') for `'d'`
    '''
    if typecode is not None and typecode not in SUPPORTED_TYPECODES:
      raise ValueError(
          "expected typecode to be one of %r, got %r" %
          (tuple(SUPPORTED_TYPECODES.keys()), typecode)
      )
    if step <= 0:
      raise ValueError("step should be >0, got %s" % (step,))
    if fill is None:
      if typecode == 'd':
        fill = float('nan')
      elif typecode == 'q':
        fill = 0
      else:
        raise RuntimeError(
            "no default fill value for typecode=%r" % (typecode,)
        )
    self.fspath = fspath
    # compare the file against the supplied arguments
    hdr_stat = self.stat(fspath)
    if hdr_stat is None:
      if typecode is None:
        raise ValueError(
            "no typecode supplied and no data file %r" % (fspath,)
        )
      file_bigendian = NATIVE_BIGENDIANNESS[typecode]
    else:
      file_typecode, file_bigendian = hdr_stat
      if typecode != file_typecode:
        raise ValueError(
            "typecode=%r but data file %s has typecode %r" %
            (typecode, fspath, file_typecode)
        )
    self.typecode = typecode
    self.file_bigendian = file_bigendian
    self.start = start
    self.step = step
    self.fill = fill
    self._itemsize = array(typecode).itemsize
    assert self._itemsize == 8
    struct_fmt = self.make_struct_format(typecode, self.file_bigendian)
    self._struct = Struct(struct_fmt)
    assert self._struct.size == self._itemsize
    self.modified = False

  def __str__(self):
    return "%s(%s,%r,%d:%d,%r)" % (
        type(self).__name__, shortpath(self.fspath), self.typecode, self.start,
        self.step, self.fill
    )

  @contextmanager
  def startup_shutdown(self):
    yield self
    self.flush()

  @property
  def end(self):
    ''' The end time of this array,
        computed as `self.start+len(self.array)*self.step`.
    '''
    return self.start + len(self.array) * self.step

  @staticmethod
  def make_struct_format(typecode, bigendian):
    ''' Make a `struct` format string for the data in a file.
    '''
    return ('>' if bigendian else '<') + typecode

  @property
  def header(self):
    ''' The header magic bytes.
    '''
    return self.make_header(self.typecode, self.file_bigendian)

  @classmethod
  def make_header(cls, typecode, bigendian):
    ''' Construct a header `bytes` object for `typecode` and `bigendian`.
    '''
    header_bs = (
        cls.MAGIC +
        cls.make_struct_format(typecode, bigendian).encode('ascii') + b'__'
    )
    assert len(header_bs) == cls.HEADER_LENGTH
    return header_bs

  @classmethod
  @pfx
  @typechecked
  @ensure(lambda result: result[0] in SUPPORTED_TYPECODES)
  def parse_header(cls, header_bs: bytes) -> Tuple[str, bool]:
    ''' Parse the file header record.
        Return `(typecode,bigendian)`.
    '''
    if len(header_bs) != cls.HEADER_LENGTH:
      raise ValueError(
          "expected %d bytes, got %d bytes" %
          (cls.HEADER_LENGTH, len(header_bs))
      )
    if not header_bs.startswith(cls.MAGIC):
      raise ValueError(
          "bad leading magic, expected %r, got %r" %
          (cls.MAGIC, header_bs[:len(cls.MAGIC)])
      )
    struct_endian_b, typecode_b, _1, _2 = header_bs[len(cls.MAGIC):]
    struct_endian_marker = chr(struct_endian_b)
    if struct_endian_marker == '>':
      bigendian = True
    elif struct_endian_marker == '<':
      bigendian = False
    else:
      raise ValueError(
          "invalid endian marker, expected '>' or '<', got %r" %
          (struct_endian_marker,)
      )
    typecode = chr(typecode_b)
    if typecode not in SUPPORTED_TYPECODES:
      raise ValueError(
          "unsupported typecode, expected one of %r, got %r" % (
              SUPPORTED_TYPECODES,
              typecode,
          )
      )
    if bytes((_1, _2)) != b'__':
      warning(
          "ignoring unexpected header trailer, expected %r, got %r" %
          (b'__', _1 + _2)
      )
    return typecode, bigendian

  @classmethod
  @pfx
  def stat(cls, fspath):
    ''' Read the data file header, return `(typecode,bigendian)`
        as from the `parse_header(heasder_bs)` method.
        Returns `None` if the file does not exist.
        Raises `ValueError` for an invalid header.
    '''
    # read the data file header
    try:
      with pfx_open(fspath, 'rb') as tsf:
        header_bs = tsf.read(cls.HEADER_LENGTH)
      if len(header_bs) != cls.HEADER_LENGTH:
        raise ValueError(
            "file header is the wrong length, expected %d, got %d" %
            (cls.HEADER_LENGTH, len(header_bs))
        )
    except FileNotFoundError:
      # file does not exist
      return None
    return cls.parse_header(header_bs)

  @property
  @cachedmethod
  def array(self):
    ''' The time series as an `array.array` object.
        This loads the array data from `self.fspath` on first use.
    '''
    assert not hasattr(self, '_array')
    try:
      ary = self.load_from(self.fspath, self.typecode)
    except FileNotFoundError:
      # no file, empty array
      ary = array(self.typecode)
    return ary

  def flush(self, keep_array=False):
    ''' Save the data file if `self.modified`.
    '''
    if self.modified:
      self.save()
      self.modified = False
      if not keep_array:
        self._array = None

  def save(self, fspath=None):
    ''' Save the time series to `fspath`, default `self.fspath`.
    '''
    assert self._array is not None, "array not yet loaded, nothing to save"
    if fspath is None:
      fspath = self.fspath
    self.save_to(self.array, fspath, self.file_bigendian)

  @classmethod
  @ensure(
      lambda typecode, result: typecode is None or result.typecode == typecode
  )
  def load_from(cls, fspath, typecode=None):
    ''' Load the data from `fspath`, return an `array.array(typecode)`
        containing the file data.
    '''
    ary = array(typecode)
    with pfx_open(fspath, 'rb') as tsf:
      header_bs = tsf.read(cls.HEADER_LENGTH)
      assert len(header_bs) == cls.HEADER_LENGTH
      h_typecode, h_bigendian = cls.parse_header(header_bs)
      if typecode is not None and h_typecode != typecode:
        raise ValueError(
            "expected typecode %r, file contains typecode %r" %
            (typecode, h_typecode)
        )
      flen = os.fstat(tsf.fileno()).st_size
      datalen = flen - len(header_bs)
      if flen % ary.itemsize != 0:
        warning(
            "data length:%d is not a multiple of item size:%d", datalen,
            ary.itemsize
        )
      datum_count = datalen // ary.itemsize
      ary.fromfile(tsf, datum_count)
      if h_bigendian != NATIVE_BIGENDIANNESS[h_typecode]:
        ary.byteswap()
    return ary

  @classmethod
  @typechecked
  def save_to(cls, ary, fspath: str, bigendian=Optional[bool]):
    ''' Save the array `ary` to `fspath`.
        If `bigendian` is specified, write the data in that endianness.
        The default is to use the native endianness.

        *Warning*:
        if the file endianness is not the native endianness,
        the array will be byte swapped temporarily
        during the file write operation.
        Concurrent users should avoid using the array during this function.
    '''
    native_bigendian = NATIVE_BIGENDIANNESS[ary.typecode]
    if bigendian is None:
      bigendian = native_bigendian
    header_bs = cls.make_header(ary.typecode, bigendian)
    with pfx_open(fspath, 'wb') as tsf:
      tsf.write(header_bs)
      if bigendian != native_bigendian:
        with array_byteswapped(ary):
          ary.tofile(tsf)
      else:
        ary.tofile(tsf)

  @ensure(lambda result: result >= 0)
  def array_index(self, when) -> int:
    ''' Return the array index corresponding the time UNIX time `when`.
    '''
    if when < self.start:
      raise ValueError("when:%s predates self.start:%s" % (when, self.start))
    return self.offset(when)

  def array_index_bounds(self, start, stop) -> (int, int):
    ''' Return a `(array_start,array_stop)` pair for the array indices
        between the UNIX times `start` and `stop`.

        Eample:

           >>> ts = TimeSeries('tsfile.csts', 'd', 19.1, 1.2)
           >>> ts.array_index_bounds(20,30)
           (0, 9)
    '''
    if start < self.start:
      raise IndexError(
          "start:%s must be >= self.start:%s" % (start, self.start)
      )
    return self.offset_bounds(start, stop)

  def array_indices(self, start, stop):
    ''' Return an iterable of the array indices for the UNIX times
        from `start` to `stop` from this `TimeSeries`.

        Eample:

           >>> ts = TimeSeries('tsfile.csts', 'd', 19.1, 1.2)
           >>> list(ts.array_indices(20,30))
           [0, 1, 2, 3, 4, 5, 6, 7, 8]
    '''
    return self.offset_range(start, stop)

  @typechecked
  def index_when(self, index: int):
    ''' Return the UNIX time corresponding to the array index `index`.
    '''
    if index < 0:
      raise IndexError("index:%d must be >=0" % (index,))
    return self.when(index)

  def __len__(self):
    ''' The length of the time series data,
        from `len(self.array)`.
    '''
    return len(self.array)

  def data(self, start, stop):
    ''' Return an iterable of `(when,datum)` tuples for each time `when`
        from `start` to `stop`.
    '''
    return zip(self.range(start, stop), self[start:stop])

  def __getitem__(self, when):
    ''' Return the datum for the UNIX time `when`.

        If `when` is a slice, return a list of the data
        for the times in the range `start:stop`
        as given by `self.range(start,stop)`.
    '''
    if isinstance(when, slice):
      X("WHEN SLICE = %r", when)
      start, stop, step = when.start, when.stop, when.step
      if step is not None:
        raise ValueError(
            "%s index slices may not specify a step" % (type(self).__name__,)
        )
      array = self.array
      astart, astop = self.offset_bounds(start, stop)
      return array[astart:astop]
    # avoid confusion with negative indices
    if when < 0:
      raise ValueError("invalid when:%s, must be >= 0" % (when,))
    return self.array[self.array_index(when)]

  def __setitem__(self, when, value):
    ''' Set the datum for the UNIX time `when`.
    '''
    if when < 0:
      raise ValueError("invalid when:%s, must be >= 0" % (when,))
    self.pad_to(when)
    assert isinstance(value,
                      (int, float)), "value is a %s:%r" % (type(value), value)
    self.array[self.array_index(when)] = value
    self.modified = True

  def pad_to(self, when, fill=None):
    ''' Pad the time series to store values up to the UNIX time `when`.

        The `fill` value is optional and defaults to the `fill` value
        supplied when the `TimeSeries` was initialised.
    '''
    if when < 0:
      raise ValueError("invalid when:%s, must be >= 0" % (when,))
    if fill is None:
      fill = self.fill
    ary_index = self.array_index(when)
    ary = self.array
    if ary_index >= len(ary):
      ary.extend(fill for _ in range(ary_index - len(ary) + 1))
      self.modified = True
      assert len(ary) == ary_index + 1


  def as_pd_series(self, start=None, end=None, tzname: Optional[str] = None):
    ''' Return a `pandas.Series` containing the data from `start` to `end`,
        default from `self.start` and `self.end` respectively.
    '''
    if start is None:
      start = self.start
    if end is None:
      end = self.end
    if tzname is None:
      tzname = get_default_timezone_name()
    ary = self.array
    data = ary[self.array_index(start):self.array_index(end)]
    indices = (datetime64(t, 's') for t in range(start, end, self.step))
    series = PDSeries(data, indices)
    return series

  @plotrange
  def plot(self, start, stop, *, figure, **scatter_kw):
    ''' Plot a trace on `figure:plotly.graph_objects.Figure`,
        creating it if necessary.
        Return `figure`.
    '''
    plotly = import_extra('plotly', DISTINFO)
    go = plotly.graph_objects
    xaxis = list(self.range(start, stop))
    yaxis = list(self[start:stop])
    assert len(xaxis) == len(yaxis), (
        "len(xaxis):%d != len(yaxis):%d, start=%s, stop=%s" %
        (len(xaxis), len(yaxis), start, stop)
    )
    figure.add_trace(go.Scatter(x=xaxis, y=yaxis, **scatter_kw))
    return figure

class TimespanPolicy(DBC):
  ''' A class mplementing apolicy about where to store data,
      used by `TimeSeriesPartitioned` instances
      to partition data among multiple `TimeSeries` data files.

      The most important methods are `tag_for(when)`
      which returns a label for a timestamp (eg `"2022-01"` for a monthly policy)
      and `timespan_for` which returns the per tag start and end times
      enclosing a timestamp.
  '''

  FACTORIES = {
      'annual': lambda *, timezone: TimespanPolicyAnnual(timezone=timezone),
      'monthly': lambda *, timezone: TimespanPolicyMonthly(timezone=timezone),
      'daily': lambda *, timezone: TimespanPolicyDaily(timezone=timezone),
  }
  DEFAULT_NAME = 'monthly'

  @typechecked
  def __init__(self, *, timezone: Optional[str] = None):
    ''' Initialise the policy.

        Parameters:
        * `timezone`: optional timezone name used to compute `datetime`s;
          the default is inferred from the default time zone
          using the `get_default_timezone_name` function
    '''
    if timezone is None:
      timezone = get_default_timezone_name()
    self.timezone = timezone

  def Arrow(self, when):
    ''' Return an `arrow.Arrow` instance for the UNIX time `when`
        in the policy timezone.
    '''
    return arrow.Arrow.fromtimestamp(when, tzinfo=self.timezone)

  @abstractmethod
  @ensure(lambda when, result: result[0] <= when < result[1])
  def timespan_for(self, when: Numeric) -> Tuple[Numeric, Numeric]:
    ''' A `TimespanPolicy` bracketing the UNIX time `when`.
    '''
    raise NotImplementedError

  def tag_for(self, when):
    ''' Return the default tag for the UNIX time `when`,
        which is derived from the `arrow.Arrow`
        format string `self.DEFAULT_TAG_FORMAT`.
    '''
    # TODO: is this correct? don't we want the tag from the start
    # in the specified timezone?
    return self.Arrow(when).format(self.DEFAULT_TAG_FORMAT)

  @require(lambda start, stop: start < stop)
  def tagged_spans(self, start, stop):
    ''' Generator yielding a sequence of `(tag,tag_start,tag_end)`
        covering the range `start:stop`.
    '''
    when = start
    while when < stop:
      tag = self.tag_for(when)
      tag_start, tag_end = self.timespan_for(when)
      yield tag, when, min(tag_end, stop)
      when = tag_end

  def tag_timespan(self, tag) -> Tuple[Numeric, Numeric]:
    ''' Return the start and end times for the supplied `tag`.
    '''
    return self.timespan_for(
        arrow.get(tag, self.DEFAULT_TAG_FORMAT,
                  tzinfo=self.timezone).timestamp()
    )

class TimespanPolicyDaily(TimespanPolicy):
  ''' A `TimespanPolicy` bracketing times at day boundaries.
  '''

  DEFAULT_TAG_FORMAT = 'YYYY-MM-DD'

  def timespan_for(self, when):
    ''' Return the start and end UNIX times
        (inclusive and exclusive respectively)
        bracketing the UNIX time `when`.
    '''
    a = self.Arrow(when)
    start = Arrow(a.year, a.month, a.day, tzinfo=self.timezone)
    end = start.shift(days=1)
    return start.timestamp(), end.timestamp()

class TimespanPolicyMonthly(TimespanPolicy):
  ''' A `TimespanPolicy` bracketing times at month boundaries.
  '''

  DEFAULT_TAG_FORMAT = 'YYYY-MM'

  def timespan_for(self, when):
    ''' Return the start and end UNIX times
        (inclusive and exclusive respectively)
        bracketing the UNIX time `when`.
    '''
    a = self.Arrow(when)
    start = Arrow(a.year, a.month, 1, tzinfo=self.timezone)
    end = start.shift(months=1)
    return start.timestamp(), end.timestamp()

class TimespanPolicyAnnual(TimespanPolicy):
  ''' A `TimespanPolicy` bracketing times at month boundaries.
  '''

  DEFAULT_TAG_FORMAT = 'YYYY'

  def timespan_for(self, when):
    ''' Return the start and end UNIX times
        (inclusive and exclusive respectively)
        bracketing the UNIX time `when`.
    '''
    a = self.Arrow(when)
    start = Arrow(a.year, 1, 1, tzinfo=self.timezone)
    end = start.shift(years=1)
    return start.timestamp(), end.timestamp()

class TimeSeriesDataDir(HasFSPath, MultiOpenMixin):
  ''' A directory containing a collection of `TimeSeries` data files.
  '''

  @typechecked
  def __init__(
      self,
      fspath,
      *,
      step: Optional[Numeric] = None,
      policy=None,  ##: TimespanPolicy
      timezone: Optional[str] = None,
      fstags: Optional[FSTags] = None,
  ):
    if fstags is None:
      fstags = FSTags()
    self._fstags = fstags
    tagged = fstags[fspath]
    super().__init__(tagged.fspath)
    self._config_modified = None
    config = self.config
    if step is None:
      if self.step is None:
        raise ValueError("missing step parameter and no step in config")
    elif self.step is None:
      self.step = step
    elif step != self.step:
      raise ValueError("step:%r != config.step:%r" % (step, self.step))
    timezone = timezone or self.timezone
    if policy is None:
      policy_name = config.auto.policy.name or TimespanPolicy.DEFAULT_NAME
      policy = TimespanPolicy.FACTORIES[policy_name](timezone=timezone)
    elif isinstance(policy, str):
      with Pfx("policy %r", policy):
        policy_name = policy
        policy = TimespanPolicy.FACTORIES[policy_name](timezone=timezone)
    else:
      policy_name = type(policy).__name__
    # fill in holes in the config
    if not config.auto.policy.name:
      self.policy_name = policy_name
    if not config.auto.policy.timezone:
      self.timezone = timezone
    self.policy = policy
    self.step = step
    self._tsks_by_key = {}

  def __str__(self):
    return "%s(%s,%s,%s)" % (
        type(self).__name__,
        shortpath(self.fspath),
        getattr(self, 'step', 'STEP_UNDEFINED'),
        getattr(self, 'policy', 'POLICY_UNDEFINED'),
    )

  @contextmanager
  def startup_shutdown(self):
    try:
      with self.fstags:
        yield
    finally:
      for ts in self._tsks_by_key.values():
        X("CLOSE %s", ts)
        ts.close()
      X("self._config_modified=%r", self._config_modified)
      if self._config_modified:
        self.save_config()

  @property
  def configpath(self):
    return self.pathto('config.ini')

  @property
  @cachedmethod
  def config(self):
    tags = TagSet.from_ini(
        self.configpath, type(self).__name__, missing_ok=True
    )
    self._config_modified = False
    return tags

  def save_config(self):
    self.config.save_as_ini(self.configpath, type(self).__name__)

  @property
  def policy_name(self):
    ''' The `policy.timezone` config value, usually a key from
        `TimespanPolicy.FACTORIES`.
    '''
    name = self.config.auto.policy.name
    if not name:
      name = self.DEFAULT_POLICY_NAME
      self.policy_name = name
    return name

  @policy_name.setter
  def policy_name(self, new_policy_name: str):
    ''' Set the `policy.timezone` config value, usually a key from
        `TimespanPolicy.FACTORIES`.
    '''
    if new_policy_name == 'AEST': raise RuntimeError
    self.config['policy.name'] = new_policy_name
    self._config_modified = True

  @property
  def step(self):
    ''' The `step` config value, the size of a time slot.
    '''
    return self.config.step

  @step.setter
  def step(self, new_step: Numeric):
    ''' Set the `step` config value, the size of a time slot.
    '''
    if new_step <= 0:
      raise ValueError("step must be >0, got %r" % (step,))
    self.config['step'] = new_step
    self._config_modified = True

  @property
  def timezone(self):
    ''' The `policy.timezone` config value, a timezone name.
    '''
    name = self.config.auto.policy.timezone
    if not name:
      name = get_default_timezone_name()
      self.timezone = name
    return name

  @timezone.setter
  def timezone(self, new_timezone: str):
    ''' Set the `policy.timezone` config value, a timezone name.
    '''
    self.config['policy.timezone'] = new_timezone
    self._config_modified = True

  def keys(self):
    ''' The known keys, derived from the subdirectories.
    '''
    return [
        key for key in pfx_listdir(self.fspath) if isdirpath(self.pathto(key))
    ]

  def key_typecode(self, key):
    ''' The `array` type code for `key`.
    '''
    # TODO: needs a mapping
    return 'd'

  @require(lambda key: is_clean_subpath(key) and '/' not in key)
  def __contains__(self, key):
    ''' Test if there is a subdirectory for `key`.
    '''
    return isdirpath(self.pathto(key))

  @require(lambda key: is_clean_subpath(key) and '/' not in key)
  def __getitem__(self, key):
    ''' Return the `TimeSeriesPartitioned` for `key`,
        creating its subdirectory if necessary.
    '''
    try:
      tsks = self._tsks_by_key[key]
    except KeyError:
      keypath = self.pathto(key)
      if not isdirpath(keypath):
        pfx_mkdir(keypath)
      tsks = self._tsks_by_key[key] = TimeSeriesPartitioned(
          self.pathto(key),
          self.key_typecode(key),
          step=self.step,
          policy=self.policy,
          fstags=self.fstags,
      )
      tsks.open()
    return tsks

  @plotrange
  def plot(self, start, stop, keys=None, *, figure, **scatter_kw):
    ''' Plot a trace on `figure:plotly.graph_objects.Figure`,
        creating it if necessary.
        Return `figure`.
    '''
    if keys is None:
      keys = sorted(self.keys())
    for key in keys:
      tsks = self[key]
      tsks.plot(start, stop, figure=figure, **scatter_kw)
    return figure

class TimeSeriesPartitioned(HasFSPath, TimeStepsMixin, MultiOpenMixin):
  ''' A collection of `TimeSeries` files in a subdirectory.
      We have one of these for each `TimeSeriesDataDir` key.

      This class manages a collection of files
      named by the tag from a `TimespanPolicy`,
      which dictates which tag holds the datum for a UNIX time.
  '''

  @typechecked
  @require(lambda step: step > 0)
  def __init__(
      self,
      dirpath: str,
      typecode: str,
      *,
      step: Union[int, float],
      policy,  ##: TimespanPolicy,
      start=0,
      fstags: Optional[FSTags] = None,
  ):
    ''' Initialise the `TimeSeriesPartitioned` instance.

        Parameters:
        * `dirpath`: the directory filesystem path,
          known as `.fspath` within the instance
        * `typecode`: the `array` type code for the data
        * `step`: keyword parameter specifying the width of a time slot
        * `policy`: the partitioning `TimespanPolicy`
        * `start`: the reference epoch, default `0`

        The instance requires a reference epoch
        because the `policy` start times will almost always
        not fall on exact multiples of `step`.
        The reference allows for reliable placement of times
        which fall within `step` of a partition boundary.
        For example, if `start==0` and `step==6` and a partition
        boundary came at `19` (eg due to some calendar based policy)
        then a time of `20` would fall in the partion left of the
        boundary because it belongs to the time slot commencing at `18`.
    '''
    assert isinstance(policy,
                      TimespanPolicy), "policy=%s:%r" % (type(policy), policy)
    super().__init__(dirpath)
    if fstags is None:
      fstags = FSTags()
    self.typecode = typecode
    self.policy = policy
    self.start = start
    self.step = step
    self.fstags = fstags
    self._ts_by_tag = {}

  def __str__(self):
    return "%s(%s,%r,%s,%s)" % (
        type(self).__name__,
        shortpath(self.fspath),
        self.typecode,
        self.step,
        self.policy,
    )

  @contextmanager
  def startup_shutdown(self):
    ''' Close the subsidiary `TimeSeries` instances.
    '''
    try:
      with self.fstags:
        yield
    finally:
      for ts in self._ts_by_tag.values():
        ts.close()

  def tag_for(self, when) -> str:
    ''' Return the tag for the UNIX time `when`.
    '''
    return self.policy.tag_for(self.round_down(when))

  def timespan_for(self, when):
    ''' Return the start and end UNIX times for the partition storing `when`.
    '''
    return self.policy.timespan_for(self.round_down(when))

  def subseries(self, when: Union[int, float]):
    ''' The `TimeSeries` for the UNIX time `when`.
    '''
    tag = self.tag_for(when)
    try:
      ts = self._ts_by_tag[tag]
    except KeyError:
      tag_start, tag_end = self.timespan_for(when)
      filepath = self.pathto(tag + TimeSeries.DOTEXT)
      ts = self._ts_by_tag[tag] = TimeSeries(
          filepath, self.typecode, start=tag_start, step=self.step
      )
      ts.open()
    return ts

  def __getitem__(self, when):
    return self.subseries(when)[when]

  def __setitem__(self, when, value):
    self.subseries(when)[when] = value

  def partition(self, start, stop):
    ''' Return an iterable of `(when,subseries)` for each time `when`
        from `start` to `stop`.

        This is most efficient if `whens` are ordered.
    '''
    ts = None
    tag_start = None
    tag_end = None
    for when in self.range(start, stop):
      if tag_start is not None and not tag_start <= when < tag_end:
        # different range, invalidate the current bounds
        tag_start = None
      if tag_start is None:
        ts = self.subseries(when)
        tag_start, tag_end = self.timespan_for(when)
      yield when, ts

  def setitems(self, whens, values):
    ''' Store `values` against the UNIX times `whens`.

        This is most efficient if `whens` are ordered.
    '''
    ts = None
    tag_start = None
    tag_end = None
    for when, value in zip(whens, values):
      if tag_start is not None and not tag_start <= when < tag_end:
        # different range, invalidate the current bounds
        tag_start = None
      if tag_start is None:
        ts = self.subseries(when)
        tag_start, tag_end = self.timespan_for(when)
      ts[when] = value

  @plotrange
  def plot(self, start, stop, *, figure, **scatter_kw):
    ''' Plot a trace on `figure:plotly.graph_objects.Figure`,
        creating it if necessary.
        Return `figure`.
    '''
    plotly = import_extra('plotly', DISTINFO)
    go = plotly.graph_objects
    xaxis = list(self.range(start, stop))
    yaxis = []
    for tag, tagged_start, tagged_stop in self.tagged_spans(start, stop):
      yaxis.extend(self[tag][tagged_start:tagged_stop])
    assert len(xaxis) == len(yaxis), (
        "len(xaxis):%d != len(yaxis):%d, start=%s, stop=%s" %
        (len(xaxis), len(yaxis), start, stop)
    )
    figure.add_trace(go.Scatter(x=xaxis, y=yaxis, **scatter_kw))
    return figure

if __name__ == '__main__':
  sys.exit(main(sys.argv))
