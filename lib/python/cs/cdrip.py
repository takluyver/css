#!/usr/bin/env python3
#

''' A tool for working with audio Compact Discs (CDs),
    uses the discid and musicbrainzngs modules.
'''

# Extract discid and track info from a CD as a preliminary to
# constructing a FreeDB CDDB entry. Used by cdsubmit.
# Rework of cddiscinfo in Python, since the Perl libraries aren't
# working any more; update to work on OSX and use MusicBrainz.
#	- Cameron Simpson <cs@cskk.id.au> 31mar2016
#

from contextlib import contextmanager
from getopt import GetoptError
import os
from os.path import expanduser, expandvars
from pprint import pformat
import sys
import discid
import musicbrainzngs
from typeguard import typechecked
from cs.cmdutils import BaseCommand
from cs.context import stackattrs
from cs.fstags import FSTags
from cs.logutils import warning
from cs.pfx import Pfx
from cs.resources import MultiOpenMixin
from cs.sqltags import SQLTags, SQLTaggedEntity

__version__ = '20201004-dev'

musicbrainzngs.set_useragent(__name__, __version__, os.environ['EMAIL'])

DEFAULT_CDRIP_DIR = '~/var/cdrip'

DEFAULT_MBDB_PATH = '~/var/cache/mbdb.sqlite'

def main(argv=None):
  ''' Call the command line main programme.
  '''
  return CDRipCommand().run(argv)

class CDRipCommand(BaseCommand):
  ''' 'cdrip' command line.
  '''

  GETOPT_SPEC = 'd:dev_info:f'
  USAGE_FORMAT = r'''Usage: {cmd} [-d tocdir] [-dev_info device] subcommand...
    -d tocdir Use tocdir as a directory of contents cached by discid
              In this mode the cache TOC file pathname is recited to standard
              output instead of the contents.
    -dev_info device Device to access. This may be omitted or "default" or
              "" for the default device as determined by the discid module.
    -f        Force. Read disc and consult Musicbrainz even if a toc file exists.'''

  @staticmethod
  def apply_defaults(options):
    ''' Set up the default values in `options`.
    '''
    options.tocdir = None
    options.force = False
    options.device = os.environ.get('CDRIP_DEV', "default")

  @staticmethod
  def apply_opts(opts, options):
    ''' Apply the command line options.
    '''
    for opt, val in opts:
      with Pfx(opt):
        if opt == '-d':
          options.tocdir = val
        elif opt == '-dev_info':
          options.device = val
        elif opt == '-f':
          options.force = True
        else:
          raise GetoptError("unimplemented option")

  @staticmethod
  @contextmanager
  def run_context(argv, options):
    ''' Prepare the `SQLTags` around each command invocation.
    '''
    fstags = FSTags()
    mbdb = MBDB()
    with stackattrs(options, fstags=fstags, mbdb=mbdb, verbose=True):
      with fstags:
        with mbdb:
          yield

  @staticmethod
  def cmd_toc(argv, options):
    ''' Usage: {cmd} [disc_id]
          Print a table of contents for the current disc.
    '''
    disc_id = None
    if argv:
      disc_id = argv.pop(0)
    if argv:
      raise GetoptError("extra arguments: %r" % (argv,))
    MB = options.mbdb
    if disc_id is None:
      dev_info = discid.read(device=None)
      disc_id = dev_info.id
    with Pfx("discid %s", disc_id):
      MB.toc(disc_id)

class MBDB(MultiOpenMixin):
  ''' An interface to MusicBrainz with a local `SQLTags` cache.
  '''

  VARIOUS_ARTISTS_ID = '89ad4ac3-39f7-470e-963a-56509c546377'

  def __init__(self):
    sqltags = self.sqltags = SQLTags(expanduser(expandvars(DEFAULT_MBDB_PATH)))
    with sqltags:
      sqltags.init()
    self.artists = sqltags.subdomain('artist')
    self.discs = sqltags.subdomain('disc')
    self.recordings = sqltags.subdomain('recording')

  def startup(self):
    ''' Start up the `MBDB`: open the `SQLTags`.
    '''
    self.sqltags.open()

  def shutdown(self):
    ''' Shut down the `MBDB`: close the `SQLTags`.
    '''
    self.sqltags.close()

  def artists_of(self, te):
    ''' Return the artists for `te`.
    '''
    return [self.artist(artist_id) for artist_id in te.tags['artists']]

  def recordings_of(self, te):
    ''' Return the recordings for `te`.
    '''
    return [
        self.recording(recording_id)
        for recording_id in te.tags.get('recordings', ())
    ]

  def toc(self, disc_id):
    ''' Print a table of contents for `disc_id`.
    '''
    disc = self.disc(disc_id)
    artists = self.artists_of(disc)
    print(
        "Artist: ",
        ', '.join([artist.tags['artist_name'] for artist in artists])
    )
    print("Title: ", disc.tags.title)
    for tracknum, recording in enumerate(self.recordings_of(disc), 1):
      artists = self.artists_of(recording)
      print(
          tracknum, recording.tags.title, '--',
          ', '.join([artist.tags['artist_name'] for artist in artists])
      )

  @staticmethod
  def _get(typename, db_id, includes, id_name='id', record_key=None):
    if record_key is None:
      record_key = typename
    getter_name = f'get_{typename}_by_{id_name}'
    try:
      getter = getattr(musicbrainzngs, getter_name)
    except AttributeError:
      warning(
          "no musicbrainzngs.%s: %s", getter_name,
          pformat(dir(musicbrainzngs))
      )
      raise
    with Pfx("%s(%r,includes=%r)", getter_name, db_id, includes):
      try:
        mb_info = getter(db_id, includes=includes)
      except musicbrainzngs.InvalidIncludeError:
        warning("help(%s):\n%s", getter_name, getter.__doc__)
        raise
      mb_info = mb_info[record_key]
    return mb_info

  def _tagif(self, tags, name, value):
    with self.sqltags:
      if value is not None:
        tags.set(name, value)

  @staticmethod
  def tag_from_tag_list(tags, mb_dict):
    ''' Set `tags.tag_list` from `mb_dict['tag-list'].
    '''
    tags.set(
        'tags', [tag_elem['name'] for tag_elem in mb_dict.get('tag-list', ())]
    )

  @staticmethod
  def tag_artists_from_credits(tags, mb_dict):
    ''' Set `tags.artist_ids` from `mb_dict['artist-credit']`.
    '''
    artist_credits = mb_dict.get('artist-credit')
    if artist_credits is not None:
      tags.set(
          'artists', [
              credit['artist']['id']
              for credit in artist_credits
              if not isinstance(credit, str)
          ]
      )

  @typechecked
  def artist(self, artist_id: str, force=False) -> SQLTaggedEntity:
    ''' Return the artist for `artist_id`.
    '''
    ##force = True
    te = self.artists.make(artist_id)
    tags = te.tags
    A = None
    if artist_id == self.VARIOUS_ARTISTS_ID:
      A = {
          'artist_name': 'various Artists',
      }
    else:
      includes = []
      for cached in 'tags', :
        if force or cached not in tags:
          includes.append(cached)
      if includes:
        A = self._get('artist', artist_id, includes)
    if A is not None:
      self._tagif(tags, 'artist_name', A.get('name'))
      self._tagif(tags, 'sort_name', A.get('sort-name'))
      self.tag_from_tag_list(tags, A)
    return te

  # pylint: disable=too-many-branches
  @typechecked
  def disc(self, disc_id: str, force=False) -> SQLTaggedEntity:
    ''' Return the `disc.`*disc_id* entry.
        Update from MB as required before return.
    '''
    ##force = True
    te = self.discs.make(disc_id)
    tags = te.tags
    includes = []
    for cached in 'artists', 'recordings':
      if force or cached not in tags:
        includes.append(cached)
    if includes:
      D = self._get('releases', disc_id, includes, 'discid', 'disc')
      assert D['id'] == disc_id
      found_medium = None
      found_release = None
      for release in D['release-list']:
        if found_medium:
          break
        for medium in release['medium-list']:
          if found_medium:
            break
          for disc in medium['disc-list']:
            if found_medium:
              break
            if disc['id'] == disc_id:
              # matched disc
              found_medium = medium
              found_release = release
      assert found_medium
      self._tagif(tags, 'title', found_release.get('title'))
      self.tag_artists_from_credits(tags, found_release)
      if 'recordings' in includes:
        track_list = found_medium.get('track-list')
        if not track_list:
          warning('no medium[track-list]')
        else:
          tags.set(
              'recording_ids',
              [track['recording']['id'] for track in track_list]
          )
    return te

  @typechecked
  def recording(self, recording_id: str, force=False) -> SQLTaggedEntity:
    ''' Return the recording for `recording_id`.
    '''
    ##force = True
    te = self.recordings.make(recording_id)
    tags = te.tags
    includes = []
    for cached in 'artists', 'tags':
      if force or cached not in tags:
        includes.append(cached)
    if includes:
      R = self._get('recording', recording_id, includes)
      self._tagif(tags, 'title', R.get('title'))
      self.tag_from_tag_list(tags, R)
      self.tag_artists_from_credits(tags, R)
    return te

if __name__ == '__main__':
  sys.exit(main(sys.argv))
