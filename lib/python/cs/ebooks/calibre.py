#!/usr/bin/env python3

''' Support for Calibre libraries.
'''

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache, total_ordering
from getopt import GetoptError
import os
from os.path import (
    basename,
    isabs as isabspath,
    join as joinpath,
    splitext,
)
from subprocess import run, DEVNULL, CalledProcessError
import sys
from tempfile import TemporaryDirectory

from icontract import require
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import declared_attr, relationship
from typeguard import typechecked

from cs.cmdutils import BaseCommand
from cs.context import stackattrs
from cs.deco import cachedmethod
from cs.fileutils import shortpath
from cs.lex import cutprefix
from cs.logutils import error, warning
from cs.pfx import Pfx, pfx_call
from cs.resources import MultiOpenMixin
from cs.sqlalchemy_utils import (
    ORM,
    BasicTableMixin,
    HasIdMixin,
)
from cs.tagset import TagSet
from cs.threads import locked_property
from cs.units import transcribe_bytes_geek

from cs.x import X

from . import FSPathBasedSingleton

class CalibreTree(FSPathBasedSingleton, MultiOpenMixin):
  ''' Work with a Calibre ebook tree.
  '''

  FSPATH_DEFAULT = '~/CALIBRE'
  FSPATH_ENVVAR = 'CALIBRE_LIBRARY'

  CALIBRE_BINDIR_DEFAULT = '/Applications/calibre.app/Contents/MacOS'

  @contextmanager
  def startup_shutdown(self):
    ''' Stub startup/shutdown.
    '''
    yield

  @locked_property
  def db(self):
    ''' The associated `CalibreMetadataDB` ORM,
        instantiated on demand.
    '''
    return CalibreMetadataDB(self)

  def dbshell(self):
    ''' Interactive db shell.
    '''
    return self.db.shell()

  @typechecked
  def __getitem__(self, dbid: int):
    return self.book_by_dbid(dbid)

  @lru_cache(maxsize=None)
  @typechecked
  @require(lambda dbid: dbid > 0)
  def book_by_dbid(self, dbid: int, *, db_book=None):
    ''' Return a cached `CalibreBook` for `dbid`.
    '''
    return CalibreBook(self, dbid, db_book=db_book)

  def __iter__(self):
    ''' Generator yielding `CalibreBook`s.
    '''
    db = self.db
    with db.db_session() as session:
      for author in sorted(db.authors.lookup(session=session)):
        with Pfx("%d:%s", author.id, author.name):
          for book in sorted(author.books):
            yield self.book_by_dbid(book.id, db_book=book)

  def by_identifier(self, type_, value):
    ''' Generator yielding `CalibreBook`
        matching the provided `(type,val)` identifier.
    '''
    db = self.db
    with db.db_session() as session:
      for identifier in db.identifiers.lookup(session=session, type=type_,
                                              val=value):
        yield self[identifier.book_id]

  def by_asin(self, asin):
    ''' Return an iterable of `CalibreBook`s with the supplied ASIN.
    '''
    return self.by_identifier('mobi-asin', asin.upper())

  def _run(self, *calargv, subp_options=None):
    ''' Run a Calibre utility command.

        Parameters:
        * `calargv`: an iterable of the calibre command to issue;
          if the command name is not an absolute path
          it is expected to come from `self.CALIBRE_BINDIR_DEFAULT`
        * `subp_options`: optional mapping of keyword arguments
          to pass to `subprocess.run`
    '''
    X("calargv=%r", calargv)
    if subp_options is None:
      subp_options = {}
    subp_options.setdefault('check', True)
    cmd, *calargv = calargv
    if not isabspath(cmd):
      cmd = joinpath(self.CALIBRE_BINDIR_DEFAULT, cmd)
    print("RUN", cmd, *calargv)
    try:
      cp = pfx_call(run, [cmd, *calargv], **subp_options)
    except CalledProcessError as cpe:
      error(
          "run fails, exit code %s:\n  %s",
          cpe.returncode,
          ' '.join(map(repr, cpe.cmd)),
      )
      if cpe.stderr:
        print(cpe.stderr.replace('\n', '  \n'), file=sys.stderr)
      raise
    return cp

  def calibredb(self, dbcmd, *argv, subp_options=None):
    ''' Run `dbcmd` via the `calibredb` command.
    '''
    return self._run(
        'calibredb',
        dbcmd,
        '--library-path=' + self.fspath,
        *argv,
        subp_options=subp_options
    )

  def add(self, bookpath):
    ''' Add a book file via the `calibredb add` command.
        Return the database id.
    '''
    cp = self.calibredb(
        'add',
        '--duplicates',
        bookpath,
        subp_options=dict(stdin=DEVNULL, capture_output=True, text=True)
    )
    # Extract the database id from the "calibredb add" output.
    dbids = []
    for line in cp.stdout.split('\n'):
      line_sfx = cutprefix(line, 'Added book ids:')
      if line_sfx is not line:
        dbids.extend(map(lambda s: int(s.strip()), line_sfx.split(',')))
    dbid, = dbids  # pylint: disable=unbalanced-tuple-unpacking
    return dbid

  @typechecked
  def add_format(self, bookpath: str, dbid: int, *, force: bool = False):
    ''' Add a book file to the existing book entry with database id `dbid`
        via the `calibredb add_format` command.

        Parameters:
        * `bookpath`: filesystem path to the source MOBI file
        * `dbid`: the Calibre database id
        * `force`: replace an existing format if already present, default `False`
    '''
    self.calibredb(
        'add_format',
        *(() if force else ('--dont-replace',)),
        str(dbid),
        bookpath,
        subp_options=dict(stdin=DEVNULL),
    )

class CalibreBook:
  ''' A reference to a book in a Calibre library.
  '''

  @typechecked
  def __init__(self, tree: CalibreTree, dbid: int, *, db_book=None):
    self.tree = tree
    self.dbid = dbid
    self._db_book = db_book

  def __str__(self):
    return f"{self.title} ({self.dbid})"

  @cachedmethod
  def db_book(self):
    ''' Return a cached reference to the database book record.
    '''
    db = self.tree.db
    with db.db_session() as session:
      X("FETCH BOOK %r", self.dbid)
      return db.books.by_id(self.dbid, session=session)

  def __getattr__(self, attr):
    ''' Unknown public attributes defer to the database record.
    '''
    if attr.startswith('_'):
      raise AttributeError(attr)
    return getattr(self.db_book(), attr)

  @property
  def mobi_subpath(self):
    ''' The subpath of a Mobi format book file, or `None`.
    '''
    formats = self.formats_as_dict()
    for fmtk in 'MOBI', 'AZW3', 'AZW':
      try:
        return formats[fmtk]
      except KeyError:
        pass
    return None

  def make_cbz(self, replace_format=False):
    ''' Create a CBZ format from the AZW3 Mobi format.
    '''
    from .mobi import Mobi  # pylint: disable=import-outside-toplevel
    calibre = self.tree
    formats = self.formats_as_dict()
    if 'CBZ' in formats and not replace_format:
      warning("format CBZ already present, not adding")
    else:
      mobi_subpath = self.mobi_subpath
      if mobi_subpath:
        mobipath = calibre.pathto(mobi_subpath)
        base, _ = splitext(basename(mobipath))
        MB = Mobi(mobipath)
        with TemporaryDirectory() as tmpdirpath:
          cbzpath = joinpath(tmpdirpath, base + '.cbz')
          pfx_call(MB.make_cbz, cbzpath)
          calibre.add_format(cbzpath, self.dbid, force=replace_format)
      else:
        raise ValueError(
            "no AZW3, AZW or MOBI format from which to construct a CBZ"
        )

class CalibreMetadataDB(ORM):
  ''' An ORM to access the Calibre `metadata.db` SQLite database.
  '''

  DB_FILENAME = 'metadata.db'

  def __init__(self, tree):
    if isinstance(tree, str):
      tree = CalibreTree(tree)
    self.tree = tree
    self.db_url = 'sqlite:///' + self.db_path
    super().__init__(self.db_url)

  @property
  def orm(self):
    ''' No distinct ORM class for `CalibreMetadataDB`.
    '''
    return self

  @property
  def db_path(self):
    ''' The filesystem path to the database.
    '''
    return self.tree.pathto(self.DB_FILENAME)

  def shell(self):
    ''' Interactive db shell.
    '''
    print("sqlite3", self.db_path)
    run(['sqlite3', self.db_path], check=True)
    return 0

  # lifted from SQLTags
  @contextmanager
  def db_session(self, *, new=False):
    ''' Context manager to obtain a db session if required
        (or if `new` is true).
    '''
    orm_state = self.orm.sqla_state
    get_session = orm_state.new_session if new else orm_state.auto_session
    with get_session() as session2:
      yield session2

  def declare_schema(self):
    r''' Define the database schema / ORM mapping.

        Database schema queried thus:

            sqlite3 ~/CALIBRE/metadata.db .schema
    '''
    Base = self.Base

    class _CalibreTable(BasicTableMixin, HasIdMixin):
      ''' Base class for Calibre tables.
      '''

    def _linktable(left_name, right_name, **addtional_columns):
      ''' Prepare and return a Calibre link table base class.

          Parameters:
          * `left_name`: the left hand entity, lowercase, singular,
            example `'book'`
          * `right_name`: the right hand entity, lowercase, singular,
            example `'author'`
          * `addtional_columns`: other keyword parameters
            define further `Column`s and relationships
      '''

      class linktable(_CalibreTable):
        ''' Prepare a `_CalibreTable` subclass representing a Calibre link table.
        '''

        __tablename__ = f'{left_name}s_{right_name}s_link'

      setattr(
          linktable, f'{left_name}_id',
          declared_attr(
              lambda self: Column(
                  left_name,
                  ForeignKey(f'{left_name}s.id'),
                  primary_key=True,
              )
          )
      )
      setattr(
          linktable, left_name,
          declared_attr(
              lambda self: relationship(
                  f'{left_name.title()}s',
                  back_populates=f'{right_name}_links',
              )
          )
      )
      setattr(
          linktable, f'{right_name}_id',
          declared_attr(
              lambda self: Column(
                  right_name,
                  ForeignKey(f'{right_name}s.id'),
                  primary_key=True,
              )
          )
      )
      setattr(
          linktable, right_name,
          declared_attr(
              lambda self: relationship(
                  f'{right_name.title()}s',
                  back_populates=f'{left_name}_links',
              )
          )
      )
      for colname, colspec in addtional_columns.items():
        setattr(
            linktable,
            colname,
            declared_attr(lambda self, colspec=colspec: colspec),
        )
      return linktable

    @total_ordering
    class Authors(Base, _CalibreTable):
      ''' An author.
      '''
      __tablename__ = 'authors'
      name = Column(String, nullable=False, unique=True)
      sort = Column(String)
      link = Column(String, nullable=False, default="")

      def __hash__(self):
        return self.id

      def __eq__(self, other):
        return self.id == other.id

      def __lt__(self, other):
        return self.sort.lower() < other.sort.lower()

    @total_ordering
    class Books(Base, _CalibreTable):
      ''' A book.
      '''
      __tablename__ = 'books'
      title = Column(String, nullable=False, unique=True, default='unknown')
      sort = Column(String)
      timestamp = Column(DateTime)
      pubdate = Column(DateTime)
      series_index = Column(Float, nullable=False, default=1.0)
      author_sort = Column(String)
      isbn = Column(String, default="")
      lccn = Column(String, default="")
      path = Column(String, nullable=False, default="")
      flags = Column(Integer, nullable=False, default=1)
      uuid = Column(String)
      has_cover = Column(Boolean, default=False)
      last_modified = Column(
          DateTime,
          nullable=False,
          default=datetime(2000, 1, 1, tzinfo=timezone.utc)
      )

      def __hash__(self):
        return self.id

      def __eq__(self, other):
        return self.id == other.id

      def __lt__(self, other):
        return self.author_sort.lower() < other.author_sort.lower()

      def identifiers_as_dict(self):
        ''' Return a `dict` mapping identifier types to values.
        '''
        return {
            identifier.type: identifier.val
            for identifier in self.identifiers
        }

      def formats_as_dict(self):
        ''' Return a `dict` mapping formats to book format relative paths.
        '''
        return {
            format.format:
            joinpath(self.path, f'{format.name}.{format.format.lower()}')
            for format in self.formats
        }

    class Data(Base, _CalibreTable):
      ''' Data files associated with a book.
      '''
      __tablename__ = 'data'
      book_id = Column(
          "book", ForeignKey('books.id'), nullable=False, primary_key=True
      )
      format = Column(String, nullable=False, primary_key=True)
      uncompressed_size = Column(Integer, nullable=False)
      name = Column(String, nullable=False)

    class Identifiers(Base, _CalibreTable):
      ''' Identifiers associated with a book such as `"isbn"` or `"mobi-asin"`.
      '''
      __tablename__ = 'identifiers'
      book_id = Column("book", ForeignKey('books.id'), nullable=False)
      type = Column(String, nullable=False, default="isbn")
      val = Column(String, nullable=None)

    class Languages(Base, _CalibreTable):
      ''' Lamguage codes.
      '''
      __tablename__ = 'languages'
      lang_code = Column(String, nullable=False, unique=True)

    class BooksAuthorsLink(Base, _linktable('book', 'author')):
      ''' Link table between `Books` and `Authors`.
      '''

    ##class BooksLanguagesLink(Base, _linktable('book', 'lang_code')):
    ##  item_order = Column(Integer, nullable=False, default=1)

    Authors.book_links = relationship(BooksAuthorsLink)
    Authors.books = association_proxy('book_links', 'book')

    Books.author_links = relationship(BooksAuthorsLink)
    Books.authors = association_proxy('author_links', 'author')
    Books.identifiers = relationship(Identifiers)
    Books.formats = relationship(Data, backref="book")

    ##Books.language_links = relationship(BooksLanguagesLink)
    ##Books.languages = association_proxy('languages_links', 'languages')

    Identifiers.book = relationship(Books, back_populates="identifiers")

    # references to table definitions
    self.authors = Authors
    self.books = Books
    self.identifiers = Identifiers
    self.languages = Languages

class CalibreCommand(BaseCommand):
  ''' Command line tool to interact with a Calibre filesystem tree.
  '''

  GETOPT_SPEC = 'C:K:'

  USAGE_FORMAT = '''Usage: {cmd} [-C calibre_library] [-K kindle-library-path] subcommand [...]
  -C calibre_library
    Specify calibre library location.
  -K kindle_library
    Specify kindle library location.'''

  SUBCOMMAND_ARGV_DEFAULT = 'ls'

  DEFAULT_LINK_IDENTIFIER = 'mobi-asin'

  USAGE_KEYWORDS = {
      'DEFAULT_LINK_IDENTIFIER': DEFAULT_LINK_IDENTIFIER,
  }

  def apply_defaults(self):
    ''' Set up the default values in `options`.
    '''
    options = self.options
    options.kindle_path = None
    options.calibre_path = None

  def apply_opt(self, opt, val):
    ''' Apply a command line option.
    '''
    options = self.options
    if opt == '-C':
      options.calibre_path = val
    elif opt == '-K':
      options.kindle_path = val
    else:
      super().apply_opt(opt, val)

  @contextmanager
  def run_context(self):
    ''' Prepare the `SQLTags` around each command invocation.
    '''
    from .kindle import KindleTree  # pylint: disable=import-outside-toplevel
    options = self.options
    with KindleTree(options.kindle_path) as kt:
      with CalibreTree(options.calibre_path) as cal:
        db = cal.db
        with db.db_session() as session:
          with stackattrs(options, kindle=kt, calibre=cal, db=db,
                          session=session, verbose=True):
            yield

  def cmd_make_cbz(self, argv):
    ''' Usage: {cmd} dbids...
    '''
    if not argv:
      raise GetoptError("missing dbids")
    options = self.options
    calibre = options.calibre
    xit = 0
    for dbid_s in argv:
      with Pfx(dbid_s):
        try:
          dbid = int(dbid_s)
        except ValueError as e:
          warning("invalid dbid: %s", e)
          xit = 1
          continue
        cbook = calibre[dbid]
        with Pfx("%s: make_cbz", cbook.title):
          cbook.make_cbz()
    return xit

  def cmd_dbshell(self, argv):
    ''' Usage: {cmd}
          Start an interactive database prompt.
    '''
    if argv:
      raise GetoptError("extra arguments: %r" % (argv,))
    return self.options.calibre.dbshell()

  def cmd_import_from_calibre(self, argv):
    ''' Usage: {cmd} other-library [identifier-name] [identifier-values...]
          Import formats from another Calibre library.
          other-library: the path to another Calibre library tree
          identifier-name: the key on which to link matching books;
            the default is {DEFAULT_LINK_IDENTIFIER}
          identifier-values: specific book identifiers to import
    '''
    options = self.options
    calibre = options.calibre
    if not argv:
      raise GetoptError("missing other-library")
    other_library = CalibreTree(argv.pop(0))
    with Pfx(shortpath(other_library.fspath)):
      if other_library is calibre:
        raise GetoptError("cannot import from the same library")
      if argv:
        identifier_name = argv.pop(0)
      else:
        identifier_name = self.DEFAULT_LINK_IDENTIFIER
      if argv:
        identifier_values = argv
      else:
        identifier_values = sorted(
            set(
                filter(
                    lambda idv: idv is not None, (
                        cbook.identifiers_as_dict().get(identifier_name)
                        for cbook in other_library
                    )
                )
            )
        )
      xit = 0
      for identifier_value in identifier_values:
        with Pfx("%s:%s", identifier_name, identifier_value):
          obooks = list(
              other_library.by_identifier(identifier_name, identifier_value)
          )
          if not obooks:
            error("no books with this identifier")
            xit = 1
            continue
          if len(obooks) > 1:
            warning(
                "  \n".join(
                    [
                        "multiple \"other\" books with this identifier:",
                        *map(str, obooks)
                    ]
                )
            )
            xit = 1
            continue
          obook, = obooks
          cbooks = list(
              calibre.by_identifier(identifier_name, identifier_value)
          )
          if not cbooks:
            print("NEW BOOK", obook)
          elif len(cbooks) > 1:
            warning(
                "  \n".join(
                    [
                        "multiple \"local\" books with this identifier:",
                        *map(str, cbooks)
                    ]
                )
            )
            print("PULL", obook, "AS NEW BOOK")
          else:
            cbook, = cbooks
            print("MERGE", obook, "INTO", cbook)
    return xit

  def cmd_ls(self, argv):
    ''' Usage: {cmd} [-l]
          List the contents of the Calibre library.
    '''
    long = False
    if argv and argv[0] == '-l':
      long = True
      argv.pop(0)
    if argv:
      raise GetoptError("extra arguments: %r" % (argv,))
    options = self.options
    calibre = options.calibre
    for book in calibre:
      with Pfx("%d:%s", book.id, book.title):
        print(f"{book.title} ({book.dbid})")
        if long:
          print(" ", book.path)
          identifiers = book.identifiers_as_dict()
          if identifiers:
            print("   ", TagSet(identifiers))
          for fmt, subpath in book.formats_as_dict().items():
            with Pfx(fmt):
              fspath = calibre.pathto(subpath)
              size = pfx_call(os.stat, fspath).st_size
              print("   ", fmt, transcribe_bytes_geek(size), subpath)

if __name__ == '__main__':
  sys.exit(CalibreCommand(sys.argv).run())
