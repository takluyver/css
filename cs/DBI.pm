#!/usr/bin/perl
#
# Subclass of the DBI class. Supplies some handy things.
#	- Cameron Simpson <cs@zip.com.au> 27jun99
#

=head1 NAME

cs::DBI - convenience routines for working with B<DBI>

=head1 SYNOPSIS

use cs::DBI;

=head1 DESCRIPTION

An assortment of routines for doing common things with B<DBI>.

=cut

use strict qw(vars);

BEGIN { use cs::DEBUG; cs::DEBUG::using(__FILE__); }

use cs::Misc;
use DBI;

package cs::DBI;

@cs::DBI::ISA=qw(DBI);

$cs::DBI::_now=time;

=head1 GENERAL FUNCTIONS

=over 4

=item mydb()

Return a database handle for my personal mysql installation.

=cut

sub mydb()
{ if (! defined $cs::DBI::_mydb)
  { ::need(cs::Secret);
    my $s = cs::Secret::get("mysql\@$ENV{SYSTEMID}");
    my $login = $s->Value(LOGIN);
    my $password = $s->Value(PASSWORD);
    my $host = $s->Value(HOST);	$host='mysql' if ! defined $host;
    ## warn "$login\@$host: $password\n";

    $cs::DBI::_mydb=DBI->connect("dbi:mysql:CS_DB:$host",$login,$password);
  }
  ## $cs::DBI::_mydb->trace(1,"$ENV{HOME}/tmp/mydb.trace");
  $cs::DBI::_mydb;
}

=item isodate(I<gmt>)

Return an ISO date string (B<I<yyyy>-I<mm>-I<dd>>)
for the supplied UNIX B<time_t> I<gmt>.
If not supplied, I<gmt> defaults to now.

=cut

# ISO date from GMT
sub isodate
{ my($gmt)=@_;
  $gmt=$cs::DBI::_now if ! defined $gmt;

  my @tm = localtime($gmt);

  sprintf("%d-%02d-%02d", $tm[5]+1900, $tm[4]+1, $tm[3]);
}

=item hashtable(I<dbh>,I<table>,I<keyfield>)

Return a reference to a hash tied to a database table,
which may then be manipulated directly.
This is not as efficient as doing bulk changes via SQL
(because every manipulation of the table
does the matching SQL, incurring much latency)
but it's very convenient.

=cut

sub hashtable($$$)
{ my($dbh,$table,$keyfield)=@_;

  my $h = {};

  ::need(cs::DBI::Table::Hash);

  if (! tie %$h, cs::DBI::Table::Hash, $dbh, $table, $keyfield)
  { return undef;
  }

  $h;
}

=item sql(I<dbh>,I<sql>)

Return a statement handle for the SQL command I<sql> applied to database I<dbh>.
This handle is cached for later reuse.

=cut

# return a statement handle for some sql
# it is cached for speed, since most SQL gets reused
sub sql($$)
{ my($dbh,$sql)=@_;
  if (! defined $dbh)
  { my @c=caller;warn "dbh UNDEF from [@c]";
  }

  my $stkey="$dbh $sql";

  ## warn "sql($dbh,\"$sql\")";
  return $cs::DBI::_cachedQuery{$stkey}
	if defined $cs::DBI::_cachedQuery{$stkey};

  $cs::DBI::_cachedQuery{$stkey} = $dbh->prepare($sql);
}

=item dosql(I<dbh>,I<sql>,I<execute-args...>)

Perform the SQL command I<sql> on database I<dbh> with the
I<execute-args> supplied.

=cut

# as above, but then perfomrs the statement handle's execute() method
#	dosql(dbh,sql,execute-args)
sub dosql
{ my($dbh,$sql)=(shift,shift);

  my $sth = sql($dbh,$sql);
  return undef if ! defined $sth;

  $sth->execute(@_)
  ? $sth : undef;
}

=item query(I<dbh>,I<table>,I<fields...>)

Return a statement handle to query I<table> in database I<dbh>
where all the specified I<fields> have specific values
(values to be supplied when the statement handle is executed).

=cut

# exact select on multiple fields
# returned statement handle for query to fetch specified records
sub query
{ my($dbh,$table)=(shift,shift);
  if (! defined $dbh)
  { my @c=caller;warn "dbh UNDEF from [@c]";
  }

  my $query = "SELECT * FROM $table";
  my $sep   = " WHERE ";

  for my $field (@_)
  { $query.="$sep$field = ?";
    $sep=" AND ";
  }

  sql($dbh, $query);
}

=item fetchall_hashref(I<sth>,I<execute-args...>)

Execute the statement handle I<sth> with the supplied I<execute-args>,
returning an array of hashrefs representing matching rows.

=cut

# execute statement handle and return all results as array of records
sub fetchall_hashref
{ my($sth)=shift;

  if (! wantarray)
  { my(@c)=caller;
    die "$0: cs::DBI::fetchall_hashref not in array context from [@c]";
  }

  return () if ! $sth->execute(@_);

  my $h;
  my @rows = ();
  while (defined ($h=$sth->fetchrow_hashref()))
  { push(@rows,$h);
  }

  @rows;
}

=item find(I<dbh>,I<table>,I<field>,I<value>)

Return an array of matching row hashrefs from I<table> in I<dbh>
where I<field> = I<value>.

=cut

# return records WHERE $field == $key
sub find($$$$)
{ my($dbh,$table,$field,$key)=@_;

  ## warn "find $table.$field = $key";
  if (! wantarray)
  { my(@c)=caller;
    die "$0: cs::DBI::find not in array context from [@c]"; 
  }

  my $sth = query($dbh,$table,$field);
  if (! defined $sth)
  { warn "$::cmd: can't make query on $dbh.$table where $field = ?";
    return ();
  }

  ## warn "doing fetchall";
  fetchall_hashref($sth,$key);
}

=item findWhen(I<dbh>,I<table>,I<field>,I<value>,I<when>)

Return an array of matching row hashrefs from I<table> in I<dbh>
where I<field> = I<value>
and the columns START_DATE and END_DATE span the time I<when>.
The argument I<when> is optional and defaults to today.

=cut

# return records WHERE $field == $key and the START/END_DATEs overlap $when
sub findWhen($$$$;$)
{ my($dbh,$table,$field,$key,$when)=@_;
  $when = isodate() if ! defined $when;

  my $sth = sql($dbh,"SELECT * FROM $table where $field = ? AND START_DATE <= ? AND (ISNULL(END_DATE) OR END_DATE >= ?)");
  return () if ! defined $sth;

  fetchall_hashref($sth,$key,$when,$when);
}

=item when(I<dbh>,I<table>,I<when>)

Return an array of matching row hashrefs from I<table> in I<dbh>
ehere the columns START_DATE and END_DATE span the time I<when>.
The argument I<when> is optional and defaults to today.

=cut

# return records WHERE the START/END_DATEs overlap $when
sub when($$;$)
{ my($dbh,$table,$when)=@_;
  $when = isodate() if ! defined $when;

  my $sth = sql($dbh,"SELECT * FROM $table where START_DATE <= ? AND (ISNULL(END_DATE) OR END_DATE >= ?)");
  return () if ! defined $sth;

  fetchall_hashref($sth,$when,$when);
}

=item updateField(I<dbh>,I<table>,I<field>,I<value>,I<where-field,where-value,...>)

Set the I<field> to I<value> in the I<table> in database I<dbh>
for records where the specified I<where-field> = I<where-value> pairs
all match.

=cut

# update fields in a table
# extra values are (f,v) for conjunctive "WHERE f = v" pairs
sub updateField
{ my($dbh,$table,$field,$value)=(shift,shift,shift,shift);

  my $sql = "UPDATE $table SET $field = ?";
  ## warn "\@_=[@_]";
  my @args = $value;
  my $sep = " WHERE ";
  while (@_ >= 2)
  { my($f,$v)=(shift,shift);
    ## warn "f=$f, v=$v";
    $sql.=$sep."$f = ?";
    push(@args,$v);
    $sep=" AND ";
  }

  ## warn "sql=[$sql], args=[@args]";

  dosql($dbh,$sql,@args);
}

=item lock_table(I<dbh>,I<table>)

Lock the specified I<table> in database I<dbh>.

=cut

sub lock_table($$)
{ my($dbh,$table)=@_;
  dosql($dbh,"LOCK TABLES $table WRITE");
}

=item unlock_tables(I<dbh>)

Release all locks held in database I<dbh>.

=cut

sub unlock_tables($)
{ my($dbh)=@_;
  dosql($dbh,"UNLOCK TABLES");
}

# return a statement handle with conjunctive WHERE constraints
# returns an ARRAY
#	empty on error
#	(sth, @args) if ok
sub sqlWhere
{ my($dbh,$sql,@w)=@_;

  my ($fullsql,@args) = sqlWhereText($sql,@w);
  my $sth = sql($dbh,$fullsql);
  return () if ! defined $sth;

  ($sth,@args);
}

sub sqlWhereText
{ my($sql,@w)=@_;

  my $sep = ' WHERE ';
  my @args = ();
  while (@w >= 2)
  { my($f,$v)=(shift(@w), shift(@w));
    push(@args,$v);
    $sql.=$sep."$f = ?";
    $sep=' AND ';
  }

  return ($sql, @args);
}

sub addDatedRecord
{ my($dbh,$table,$when,$rec,@delwhere)=@_;
  if (! defined $when)
  { my(@c)=caller;
    die "$::cmd: cs::DBI::addDatedRecord($table): \$when undefined from [@c]";
  }

  if (@delwhere)
  # delete old records first
  { my ($sth, @args) = sqlWhere($dbh,'DELETE FROM $table',@delwhere);
    if (! defined $sth)
    { warn "$::cmd: cs::DBI::addDatedRecord($table): can't make sql to delete old records";
      return undef;
    }

    $sth->execute(@args);
  }

  $rec->{START_DATE}=$when;
  cs::DBI::insert(MSQ::tsdb(),$table, keys %$rec)->ExecuteWithRec($rec);
}

sub delDatedRecord
{ my($dbh,$table,$when,@delwhere)=@_;
  if (! defined $when)
  { my(@c)=caller;
    die "$::cmd: cs::DBI::delDatedRecord($table): \$when undefined from [@c]";
  }
  if (@delwhere < 2)
  { my(@c)=caller;
    die "$::cmd: cs::DBI::delDatedRecord($table): no \@delwhere from [@c]";
  }

  # set closing date of the day before the deletion day
  my $today = new cs::Day $when;
  my $prevwhen = $today->Prev()->Code();

  my ($sql, @args) = sqlWhereText("UPDATE $table SET END_DATE = ?", @delwhere);
  $sql .= " AND START_DATE <= ? AND ISNULL(END_DATE)";

  my $sth = sql($dbh, $sql);
  if (! defined $sth)
  { warn "$::cmd: cs::DBI::delDatedRecord($table): can't make sql to delete old records";
    return undef;
  }

  $sth->execute($prevwhen,@args,$when);
}

=item last_id()

Return the id of the last item inserted with B<ExecuteWithRec> below.

=cut

# return the id if the last record inserted by ExecuteWithRec
undef $cs::DBI::_last_id;
sub last_id()
{ $cs::DBI::_last_id;
}

=back

=head1 OBJECT CREATION

=over 4

=item insert(I<dbh>,I<table>,I<dfltok>,I<fields...>)

Create a new B<cs::DBI> object
for insertion of rows into I<table> in database I<dbh>.
If the parameter I<dfltok> is supplied as 0 or 1
it governs whether it is permissible for the inserted rows
to lack values for the I<fields> named;
the default is for all named I<fields> to be required.
Once created,
this object may be used with the
B<ExecuteWithRec> method below.

=cut

# return a cs::DBI object which accepts the ExecuteWithRec method,
# used to insert records
sub insert	# dbh,table[,dfltok],fields...
{ my($dbh,$table)=(shift,shift);
  my $dfltok=0;
  if (@_ && $_[0] =~ /^[01]$/)
  { $dfltok=shift(@_)+0;
  }
  my @fields = @_;

  my $sql = "INSERT INTO $table ("
	  . join(',',@fields)
	  . ") VALUES ("
	  . join(',',map('?',@fields))
	  . ")";

  ## warn "SQL is [$sql]";
  bless [ $dbh, $table, $sql, sql($dbh,$sql), $dfltok, @fields ];
}

=back

=head1 OBJECT METHODS

=over 4

=item ExecuteWithRec(I<record-hashrefs...>)

Insert the records
described by the I<record-hashrefs>
into the appropriate table.

=cut

# takes an "insert" sql query and inserts some records
# return is undef on failure or last insertid()
sub ExecuteWithRec
{ my($isth)=shift;

  my($dbh,$table,$sql,$sth,$dfltok,@fields)=@$isth;

  ## warn "stashing record:\n".cs::Hier::h2a($rec,1);
  ## warn "sth=$sth, \@isth=[ @$isth ]\n";
  ## warn "fields=[@fields]";

  my $ok = 1;

  # hack - lock the table if we're inserting 5 or more records,
  # for speed
  my $locked = (@_ > 5 && lock_table($dbh,$table));

  INSERT:
  while (@_)
  { my $rec = shift(@_);
    ## warn "INSERT rec = ".cs::Hier::h2a($rec,1);
    my @execargs=();

    for my $field (@fields)
    { if (! exists $rec->{$field})
      { if ($dfltok)
	{ $rec->{$field}=undef;
	}
	else
	{ ::need(cs::Hier);
	  die "$::cmd: ExecuteWithRec(): no field \"$field\": rec="
	      .cs::Hier::h2a($rec,1);
	}
      }
      elsif (! defined $rec->{$field})
      { ## warn "$field = UNDEF!";
	## $rec->{$field}='';
      }

      push(@execargs, $rec->{$field});
    }

    # for some reason, text fields can't be empty - very bogus
    ## for (@execargs) { $_=' ' if defined && ! length; }


    if (! $sth->execute(@execargs))
    { warn "$::cmd: ERROR with insert";
      my @c = caller;
      warn "called from [@c]\n";
      warn "execargs=".cs::Hier::h2a(\@execargs,0)."\n";
      $ok=0;
      last INSERT;
    }
    else
    { ## warn "INSERT OK, noting insertid";
      ## XXX: was 'insertid'; may break if we ever leave mysql
      $cs::DBI::_last_id=$sth->{'mysql_insertid'};
    }
  }

  unlock_tables($dbh) if $locked;

  $ok;
}

# given a date (ISO form) select the entries from the given table
# which contain the date
sub SelectDateRanges($$$;$)
{ my($this,$table,$constraint,$when)=@_;
  $constraint=( defined $constraint && length $constraint
	     ? "$constraint AND "
	     : ''
	     );
  $when=isodate() if ! defined $when;

  my $dbh = $this->{DBH};

  $when=$dbh->quote($when);

  my $statement = "SELECT * FROM $table WHERE $constraint START_DATE <= $when AND ( ISNULL(END_DATE) OR END_DATE >= $when )";
  ## warn "statement=[$statement]";
  dosql($dbh,$statement);
}

# as above, but return the data
sub GetDateRanges
{ my($this)=shift;

  my $sth = $this->SelectDateRanges(@_);
  return () if ! defined $sth;
  fetchall_hashref($sth);
}

=back

=head1 SEE ALSO

L<DBI>

=head1 AUTHOR

Cameron Simpson <cs@zip.com.au>

=cut

1;
