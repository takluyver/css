#!/usr/bin/perl=1;
#
# Do HTTP-related client stuff.
#	- Cameron Simpson <cs@zip.com.au>
#

use strict qw(vars);

BEGIN { use cs::DEBUG; cs::DEBUG::using(__FILE__); }

use cs::Misc;
use cs::Net::TCP;
use cs::MIME;
use cs::URL;
use cs::RFC822;

package cs::HTTP;

@cs::HTTP::ISA=qw(cs::Net::TCP);

$cs::HTTP::Debug=exists $ENV{DEBUG_HTTP} && length $ENV{DEBUG_HTTP};

$cs::HTTP::Port=80;	# default port number for HTTP

%cs::HTTP::cs::HTTP::_Auth=();

$cs::HTTP::_HTTP_VERSION='HTTP/1.0';

####################
# Response codes
#
#  Good ones
$cs::HTTP::R_OK	='200';	# request fulfilled
$cs::HTTP::R_CREATED	='201';	# follows post - text is URI of new
				# document
$cs::HTTP::R_ACCEPTED	='202';	# request accepted but not complete (may
				# never be)
$cs::HTTP::R_PARTIAL	='203';	# response is a private metaweb, not the
				# original
$cs::HTTP::R_NORESPONSE ='204';	# request ok, but nothing to send back -
				# client's browser should stay in the
				# same spot
#  Redirection
$cs::HTTP::M_MOVED	='301';	# document has new permanent URI
				# lines follow of the form:
				#	URI: <url> <comment>
$cs::HTTP::M_FOUND	='302';	# document is currently elsewhere
				# lines follow of the form:
				#	URI: <url> <comment>
$cs::HTTP::M_METHOD	='303';	# document needs different method
				# next line is:
				#	Method: <method> <url>
				#	body-section
				# body section is data for method
$cs::HTTP::M_NOT_MOD	='304';	# response to conditional GET - document not
				# modified, client should used cached version
#  Bad ones - 4xx is client error, 5xx is server error
$cs::HTTP::E_BAD	='400';	# bad request
$cs::HTTP::E_UNAUTH	='401';	# bad authorisation - text is auth scheme spec
$cs::HTTP::E_PAYMENT	='402';	# payment required - text is payment scheme spec
$cs::HTTP::E_FORBIDDEN	='403';	# request denied - authorisation won't help
$cs::HTTP::E_NOT_FOUND	='404';	# no match for URI
$cs::HTTP::E_INTERNAL	='500';	# undiagnosed server problem
$cs::HTTP::E_NOT_IMPL	='501';	# facility not supported
$cs::HTTP::E_OVERLOAD	='502';	# load too high - try later
$cs::HTTP::E_GTIMEOUT	='503';	# gateway timeout - subservices didn't
				# respond in time

sub new	# (host[,port]) -> connection
{ my($class,$host,$port,$isProxy)=@_;
  $port=$cs::HTTP::Port if ! defined $port;
  $isProxy=0 if ! defined $isProxy;

  my $this;

  ## warn "HTTP: calling new TCP($host,$port)";
  $this=new cs::Net::TCP ($host,$port);
  return undef if ! defined $this;

  $this->{HOST}=lc($host);
  $this->{PORT}=$port;
  $this->{ISPROXY}=$isProxy;

  bless $this, $class;
}

sub DESTROY
{ my($this)=@_;
  $this->SUPER::DESTROY($this);
}

# strangely, we supply everything before getting a response
# simpler, I guess
sub Request	# (method,uri,[hdrs,[data,[version-string]]])
		# -> (version, rcode, rtext, rhdrs)
{ my($this,$method,$uri,$hdrs,$data,$version)=@_;
  $version=$cs::HTTP::_HTTP_VERSION if ! defined $version;
  die "no \$uri" if ! defined $uri;
  $method=uc($method);

  my($olduri);

  if (ref $uri)	{ ($uri,$olduri)=@$uri;
		}
  elsif (defined $ENV{HTTP_REFERER})
		{ $olduri=$ENV{HTTP_REFERER};
		}
  else		{ $olduri=$uri;
		}

  # minor cleans - XXX should do something more thorough
  $uri =~ s/ /%20/g;
  $uri =~ s/\t/%09/g;
  my($U)=new cs::URL $uri;

  my($rqhdrs)=_rqhdr($uri,$olduri);
  if (defined $hdrs)
  { for ($hdrs->Hdrs())
    { $rqhdrs->Add($_,SUPERCEDE);
    }
  }

  ############################
  # Supply request and headers.
  $this->Put("$method "
	    .($this->{ISPROXY}
		? $uri
		: $U->LocalPart())
	    ." $version\r\n");
  $cs::HTTP::Debug && warn "HTTP: $method $uri $version";

  $rqhdrs->WriteItem($this->{OUT});
  $cs::HTTP::Debug && warn "HTTP: ".cs::Hier::h2a($rqhdrs,1);

  local($_);

  ############################
  # Supply data if present.
  if (defined $data)
  {
    while (defined ($_=$data->Read()) && length)
    { $this->Put($_);
    }
  }

  $this->Flush();

  ############################
  # Collect response.
  if (! defined ($_=$this->GetLine()) || ! length)
  {
    warn "EOF from HTTP server";
    return ();
  }

  chomp;	s/\r$//;
  if (! /^(http\/\d+\.\d+)\s+(\d{3})\s*/i)
  {
    warn "bad response from HTTP server: $_";
    return ();
  }

  my($rversion,$rcode,$rtext)=($1,$2,$');
  $hdrs=new cs::RFC822 $this->{IN};

  wantarray
	? ($rversion,$rcode,$rtext,$hdrs)
	: { VERSION => $rversion,
	    CODE    => $rcode,
	    TEXT    => $rtext,
	    HDRS    => $hdrs,
	  }
	;
}

# convenience
sub Get	{ my($this)=shift;
	  $this->Request(GET,@_);
	}
sub Post{ my($this,$uri,$data)=(shift,shift,shift);
	  my(@data);
	  for (keys %$data)
		{
		  push(@data,"$_=".urlEncode($data->{$_})."\n");
		}
	  my($d)=new cs::Source (ARRAY,\@data);
	  die "can't make cs::Source(ARRAY,@data)" if ! defined $d;
	  $this->Request(POST,$uri,undef,$d);
	}

sub RequestData
	{ my($this)=shift;
	  my($rversion,$rcode,$rtext,$hdrs)=$this->Request(@_);

	  return undef if ! defined $rversion || $rcode ne 200;

	  my($cte)=$hdrs->Hdr(CONTENT_TRANSFER_ENCODING);

	  defined $cte && length $cte
		? cs::MIME::decodedSource($this->{IN},$cte)
		: $this->{IN};
	}

$cs::HTTP::ptnToken='[^][\000-\037()<>@,;:\\"/?={}\s]+';
sub parseAttrs
	{ local($_)=shift;
	  my($max)=@_;

	  my($h)={};

	  my($attr,$value);

	  #           1                           2
	  while ((! defined $max || $max-- > 0)
	      && /^\s*($cs::HTTP::ptnToken)\s*=\s*("[^"]*"|$cs::HTTP::ptnToken)(\s*;)?/o)
		{ ($attr,$value)=($1,$2);
		  $_=$';

		  $attr=uc($attr);
		  $value =~ s/^"(.*)"$/$1/;

		  $h->{$attr}=$value;
		}

	  wantarray ? ($h,$_) : $h;
	}

sub _url2hpf
	{ my($url)=@_;

	  main::need(cs::URL);

	  my($URL)=new cs::URL $url;

	  return undef unless $URL->{PROTO} eq HTTP;

	  ($URL->{HOST},$URL->{PROTO},$URL->{PATH});
	}


#######################
# Grammar routines.
#

$cs::HTTP::_RangeCTL='\000-\031\177';
$cs::HTTP::_RangeTSpecial="()<>@,;:\\\"{} \t";

$cs::HTTP::_PtnToken="[^$cs::HTTP::_RangeCTL$cs::HTTP::_RangeTSpecial]+";

$cs::HTTP::_PtnLWS='(\r?\n)?[ \t]+';
$cs::HTTP::_PtnQDText="(($cs::HTTP::_PtnLWS)|[^\"$cs::HTTP::_RangeCTL])";

$cs::HTTP::_PtnQuotedString="\"($cs::HTTP::_PtnQDText)*\"";

sub token
	{ local($_)=@_;

	  return undef unless /\s*($cs::HTTP::_PtnToken)\s*/o;

	  wantarray ? ($1,$') : $1;
	}

sub quoted_string
	{ local($_)=shift;
	  my($keep_quotes)=@_;

	  return undef unless /\s*($cs::HTTP::_PtnQuotedString)\s*/o;

	  my($match,$tail)=($1,$');

	  $match=$1 if ! $keep_quotes && $match =~ /^"(($cs::HTTP::_PtnQDText)*)/o;

	  wantarray ? ($match,$tail) : $match;
	}

sub word
	{ local($_)=shift;
	  my($keep_quotes)=@_;

	  return undef unless /\s*(($cs::HTTP::_PtnToken)|($cs::HTTP::_PtnQuotedString))\s*/o;

	  my($match,$tail)=($1,$');

	  $match=$1 if ! $keep_quotes && $match =~ /^"(($cs::HTTP::_PtnQDText)*)/o;

	  wantarray ? ($match,$tail) : $match;
	}

sub tokenList	# text -> ([tok,val,...],tail)
	{ local($_)=@_;
	  my($list,$tail);
	  my($tok,$value);

	  $list=[];

	  while (( ($tok,$tail)=token($_) )
	      && $tail =~ /^=\s*/
	      && ( ($value,$tail)=quoted_string($') )
		)
		{ push(@$list,$tok,$value);
		  $_=$tail;
		  last TOKEN unless /^,\s*/;
		  $_=$';
		}

	  wantarray ? ($list,$_) : $list;
	}

#######################
# Authorisation code.
#

@cs::HTTP::_Auth=();
sub addAuthority
	{ my($host,$port,$pathpfx)=@_;

	  push(@$cs::HTTP::_Auth,
		{ HOST => $host, PORT => $port, PATH => $pathpfx });
	}

sub findAuthority
{ my($host,$port,$path)=@_;
  my($ref);

  for (@cs::HTTP::_Auth)
	{ if ($host eq $_->{HOST}
	   && $port == $_->{PORT}
	   && length($path) >= length $_->{PATH}
	   && substr($path,$[,length($_->{PATH})) eq $_->{PATH}
	   && ( ! defined $ref
	     || length $ref->{PATH} < length $_->{PATH}
	      )
	     )
		{ $ref=$_;
		}
	}

    $ref;
  }

sub hexify($$)
{ my($str,$hexchptn)=@_;

  if ($hexchptn eq HTML) { $hexchptn='[^!-~]|"'; }

  $str =~ s/$hexchptn/sprintf("%%%02x",ord($&))/eg;

  $str;
}

sub unhexify($)
{ my($str)=@_;
  $str =~ s/%([\da-f][\da-f])/chr(hex($1))/egi;
  $str;
}

sub _rqhdr
{ my($targetURL,$srcURL)=@_;
  $srcURL=defined($ENV{HTTP_REFERER}) ? $ENV{HTTP_REFERER} : $targetURL
	if ! defined $srcURL;

  my($U)=new cs::URL $targetURL;
  my($rqhdrs)=new cs::RFC822;

  $rqhdrs->Add([ACCEPT,"*/*"]);
  $rqhdrs->Add([REFERER,$srcURL]);
  $rqhdrs->Add([HOST,$U->{HOST}]) if exists $U->{HOST};

  $rqhdrs;
}

1;
