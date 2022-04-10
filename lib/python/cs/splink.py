#!/usr/bin/env python3

''' Assorted utility functions for working with data from Selectronics' SP-LINK programme which communicates with their controllers.
'''

import arrow
from datetime import datetime
from fnmatch import fnmatch
from functools import partial
import os
from os.path import join as joinpath
import sys

import pytz

from cs.cmdutils import BaseCommand
from cs.csvutils import csv_import
from cs.pfx import pfx_call
from cs.timeseries import (
    TimeSeriesDataDir,
    TimespanPolicyAnnual,
    get_default_timezone_name,
)

from cs.x import X

pfx_listdir = partial(pfx_call, os.listdir)

SPLINK_LOG_INTERVAL = 900  # really? 15 minutes? ugh

def main(argv=None):
  return SPLinkCommand(argv).run()

class SPLinkCommand(BaseCommand):

  def cmd_import(self, argv):
    csv_dirpath, tsdirpath = argv
    tsd = TimeSeriesDataDir(tsdirpath, step=900, policy=TimespanPolicyAnnual())
    import_csv_data(csv_dirpath, tsd)

def ts2000_unixtime(tzname=None):
  ''' Convert an SP-Link seconds-since-2000-01-01-local-time offset
      into a UNIX time.
  '''
  if tzname is None:
    tzname = 'local'
  a2000 = arrow.get(datetime(2000, 1, 1, 0, 0, 0), tzname)
  unixtime = a2000.timestamp()
  X("a2000 %s, unixtime %s", a2000, unixtime)
  return unixtime

def import_csv_data(csv_dirpath: str, tsd: TimeSeriesDataDir, tzname=None):
  ''' Read the CSV files in `csv_dirpath` and import them into a
      `TimeSeriesDataDir`.
  '''
  nan = float('nan')
  ts2000 = ts2000_unixtime(tzname)
  X("ts2000 = %s", ts2000)
  # load the DetailedData CSV
  detailed_csvfilename, = [
      filename for filename in pfx_listdir(csv_dirpath)
      if fnmatch(filename, '*_DetailedData_????-??-??_??-??-??.CSV')
  ]
  csvpath = joinpath(csv_dirpath, detailed_csvfilename)
  rowtype, rows = csv_import(csvpath)
  # group the values by key
  keys = rowtype.attributes_
  key0 = keys[0]
  key_values = {key: [] for key in keys}
  for row in rows:
    for key, value in zip(keys, row):
      if key == key0:
        # seconds since 2000-01-01; make UNIX time
        value = int(value) + ts2000
      else:
        try:
          value = int(value)
        except ValueError:
          try:
            value = float(value)
          except ValueError:
            value = nan
      key_values[key].append(value)
  X("len %r = %d", key0, len(key_values[key0]))
  for key in keys[2:]:
    X("key = %s", key)
    tsks = tsd[key]
    tsks.setitems(key_values[key0], key_values[key])

if __name__ == '__main__':
  sys.exit(main(sys.argv))
