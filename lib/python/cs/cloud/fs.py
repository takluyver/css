#!/usr/bin/env python3

''' BackBlaze B2 support.
'''

import os
from os.path import join as joinpath
from icontract import require
from typeguard import typechecked
from cs.obj import SingletonMixin, as_dict
from cs.pfx import pfx_method, XP
from . import Cloud

class FSCloud(SingletonMixin, Cloud):
  ''' A filesystem handle.
  '''

  PREFIX = 'fs'

  @staticmethod
  @require(lambda credentials: credentials is None)
  def _singleton_key(credentials):
    return credentials

  @require(lambda credentials: credentials is None)
  def __init__(self, credentials):
    assert credentials is None
    if hasattr(self, 'credentials'):
      return
    super().__init__(credentials)

  def __str__(self):
    return f"{self.PREFIX}:///"

  __repr__ = __str__

  def bucketpath(self, bucket_name, credentials=None):
    ''' Return the path for the supplied `bucket_name`.
        Include the `credentials` if supplied.
    '''
    assert credentials is None
    return f'{self.PREFIX}://{bucket_name}'

  @classmethod
  def parse_sitepart(cls, sitepart):
    ''' Parse the site part of an fspath, return `(credentials,bucket_name)`.
        Since filesystem paths have no credentials we just return the sitepart.
    '''
    return None, sitepart

  @classmethod
  @typechecked
  def from_sitepart(cls, sitepart: str):
    ''' Return a `B2Cloud` instance from the site part of a b2path.
    '''
    credentials, _ = cls.parse_sitepart(sitepart)
    return cls(credentials)

  @staticmethod
  def upload_buffer(
      bfr,
      bucket_name: str,
      path: str,
      file_info=None,
      content_type=None,
      progress=None,
  ):
    ''' Upload bytes from `bfr` to `path` within `bucket_name`,
        which means to the file `/`*bucket_name*`/`*path*.
        Return a `dict` with some relevant information.

        TODO: apply file_info using fstags.
    '''
    filename = os.sep + joinpath(bucket_name, path)
    with open(filename, 'wb') as f:
      for bs in bfr:
        f.write(bs)
        progress += len(bs)
    return as_dict(os.stat(filename), 'st_')