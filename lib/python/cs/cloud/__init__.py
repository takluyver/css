#!/usr/bin/env python3

''' Stuff for working with cloud storage.
'''

from abc import ABC
from collections import namedtuple
from os.path import join as joinpath
import sys
from threading import RLock
from icontract import require
from typeguard import typechecked
from cs.buffer import CornuCopyBuffer
from cs.lex import is_identifier
from cs.logutils import setup_logging
from cs.obj import SingletonMixin
from cs.pfx import Pfx
from cs.py.modules import import_module_name

from cs.x import X

def is_valid_subpath(subpath):
  ''' True if `subpath` is valid:
      * not empty
      * does not start or end with a slash (`'/'`)
      * does not contain any multiple slashes
  '''
  try:
    validate_subpath(subpath)
  except ValueError:
    return False
  return True

def validate_subpath(subpath):
  with Pfx("validate_subpath(%r)", subpath):
    if not subpath:
      raise ValueError("empty subpath")
    if subpath.startswith('/'):
      raise ValueError("subpath starts with a slash")
    if subpath.endswith('/'):
      raise ValueError("subpath ends with a slash")
    if '//' in subpath:
      raise ValueError("subpath contains a multislash")

class CloudPath(namedtuple('CloudPath',
                           'cloudcls credentials bucket_name subpath')):
  ''' A deconstructed cloud path.
  '''

  @classmethod
  @typechecked
  def from_str(cls, cloudpath: str):
    ''' Parse a cloudpath
        of the form *prefix*`://`[*credentials*`@`]*bucket_name*[`/`*subpath`]
        such as `"b2://keyId:apiKey@bucket_name/subpath"`.
        Return a `namedtuple` with fields
        `(cloudcls,credentials,bucket_name,subpath)`.
    '''
    try:
      prefix, tail = cloudpath.split('://', 1)
    except ValueError:
      raise ValueError("missing ://")
    try:
      cloudcls = Cloud.from_prefix(prefix)
    except KeyError:
      raise ValueError("unknown cloud service %r" % (prefix,))
    try:
      sitepart, subpath = tail.split('/', 1)
    except ValueError:
      sitepart, subpath = tail, None
    else:
      if subpath:
        validate_subpath(subpath)
    credentials, bucket_name = cloudcls.parse_sitepart(sitepart)
    return cls(cloudcls, credentials, bucket_name, subpath)

  def as_path(self):
    ''' The `CloudPath` as a string.
    '''
    return joinpath(
        self.cloud.bucketpath(self.bucket_name), self.subpath or ""
    )

  @property
  def cloud(self):
    ''' The cloud service supporting this path.
    '''
    return self.cloudcls(self.credentials)

class Cloud(ABC):
  ''' A cloud storage service.
  '''

  def __init__(self, credentials):
    self.credentials = credentials
    self._lock = RLock()

  @staticmethod
  @typechecked
  @require(lambda prefix: is_identifier(prefix))  # pylint: disable=unnecessary-lambda
  def from_prefix(prefix: str):
    ''' Return the `Cloud` subclass
    '''
    module_name = __name__ + '.' + prefix
    class_name = prefix.upper() + 'Cloud'
    return import_module_name(module_name, class_name)

  @abstractmethod
  def bucketpath(self, bucket_name, credentials=None):
    ''' Return the path for the supplied `bucket_name`.
        Include the `credentials` if supplied.
    '''
    raise NotImplementedError("bucketpath")

  @abstractclassmethod
  def parse_sitepart(cls, sitepart):
    ''' Parse the site part of an fspath, return `(credentials,bucket_name)`.
    '''
    raise NotImplementedError("bucketpath")

  @classmethod
  @typechecked
  def from_sitepart(cls, sitepart: str):
    ''' Return a `Cloud` instance from the site part of a cloud path.
    '''
    credentials, _ = cls.parse_sitepart(sitepart)
    return cls(credentials)

  # pylint: disable=too-many-arguments
  @abstractmethod
  def upload_buffer(
      self,
      bfr,
      bucket_name: str,
      path: str,
      file_info=None,
      content_type=None,
      progress=None,
  ):
    ''' Upload bytes from `bfr` to `path` within `bucket_name`.

        Parameters:
        * `bfr`: the source buffer
        * `bucket_name`: the bucket name
        * `path`: the subpath within the bucket
        * `file_info`: an optional mapping of extra information about the file
        * `content_type`: an optional MIME content type value
        * `progress`: an optional `cs.progress.Progress` instance
    '''
    raise NotImplementedError("upload_buffer")

class CloudArea(namedtuple('CloudArea', 'cloud bucket_name basepath')):
  ''' A storage area in a cloud bucket.
  '''

  @classmethod
  def from_cloudpath(cls, path: str):
    ''' Construct a new CloudArea from the cloud path `path`.
    '''
    CP = CloudPath.from_str(path)
    return cls(CP.cloud, CP.bucket_name, CP.subpath)

  @property
  def cloudpath(self):
    ''' The path to this storage area.
    '''
    return joinpath(self.cloud.bucketpath(self.bucket_name), self.basepath)

  def __getitem__(self, filepath):
    validate_subpath(filepath)
    return CloudAreaFile(self, filepath)

class CloudAreaFile(SingletonMixin):
  ''' A reference to a file in cloud storage area.
  '''

  @staticmethod
  def _singleton_key(cloud_area, filepath):
    validate_subpath(filepath)
    return cloud_area, filepath

  ##@typechecked
  def __init__(self, cloud_area: CloudArea, filepath: str):
    X("CAF init cloud_area=%s filepath=%r", cloud_area, filepath)
    if hasattr(self, 'filepath'):
      return
    validate_subpath(filepath)
    self.cloud_area = cloud_area
    self.filepath = filepath

  def __str__(self):
    return self.cloudpath

  @property
  def cloud(self):
    ''' The `Cloud` for the storage area.
    '''
    return self.cloud_area.cloud

  @property
  def cloudpath(self):
    ''' The cloud path for this file.
    '''
    return joinpath(self.cloud_area.cloudpath, self.filepath)

  def upload_buffer(self, bfr, *, progress=None):
    ''' Upload a buffer into the cloud to the specified `subpath`.
    '''
    self.upload_result = self.cloud.upload_buffer(
        bfr,
        self.cloud_area.bucket_name,
        joinpath(self.cloud_area.basepath, self.filepath),
        progress=progress
    )
    return self.upload_result

  def upload_filename(self, filename, *, progress=None):
    ''' Upload a local file into the cloud to the specified `subpath`.
    '''
    with open(filename, 'rb') as f:
      bfr = CornuCopyBuffer.from_fd(f.fileno())
      return self.upload_buffer(bfr, progress=progress)
