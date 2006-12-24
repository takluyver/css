_logNode=document.body;
function _log(str) {
  if (_logNode != null) {
    _logNode.appendChild(document.createTextNode(str));
    _logNode.appendChild(document.createElement("BR"));
  }
}
function _logTo(elem) {
  _logNode=elem;
}

_cs_singlePixelIMGPrefix = '';
_cs_browserVersion = parseInt(navigator.appVersion);
_cs_agent = navigator.userAgent.toLowerCase();
_cs_isGecko = (_cs_agent.indexOf(" gecko/") > 0);
_cs_isIE = (_cs_agent.indexOf(" msie ") > 0);
// _cs_isGecko = false;
// _cs_isIE = true;
//document.write("appver = "+navigator.appVersion+", agent = "+navigator.userAgent+"<BR>\n");
//document.write("isIE="+_cs_isIE+", isGecko="+_cs_isGecko+"<BR>\n");

_cs_seq = 0;
function csSeq() {
  return _cs_seq++;
}

function csPushOnresize(fn) {
  var old = window.onresize;
  window.onresize = function() {
                      if (old) old();
                      fn();
                    };
}

function csNode(type) {
  return document.createElement(type);
}

function csObjectToString() {
  var s;

  if (this instanceof Array) {
    s = "[";
    var first = true;
    for (var i=0; i<this.length; i++) {
      var e = csStringable(this[i]);
      if (first) first=false;
      else       s += ", ";
      s+=e;
    }
    s += "]";
  } else {
    // General object dump.
    s = "{";
    var first=true;
    var v;
    for (var k in this) {
      if (k == "toString") continue;
      csStringable(k)

      if (first) first=false;
      else s+=", ";

      v=csStringable(this[k]);
      s+=k+": "+v;
    }
    s+="}";
  }

  return s;
}
function csStringable(o) {
  if (!(o.toString === csObjectToString)) {
    var t = typeof(o);
    if (t != "number" && t != "string" && t != "function") {
      o.toString=csObjectToString;
    }
  }
  return o;
}

function csXY(x,y) {
  return csStringable({x: x, y: y});
}

function csSize(width, height) {
  return csStringable({width: width, height: height});
}

function box(xy, size) {
  return csStringable({x: xy.x, y: xy.y, width: size.width, height: size.height});
}

function csDIV(colour) {
  var div = csNode('DIV');
  if (colour) {
    var fillImg = csSinglePixelIMG(colour);
    csSetSize(fillImg,"100%","100%");
    csSetPosition(fillImg,csXY(0,0));
    csSetZIndex(div,0);
    csSetZIndex(fillImg,-1023);
    div.appendChild(fillImg);
  }
  return div;
}

function csText(str) {
  return document.createTextNode(str);
}

function csIMG(src,onload) {
  var img = csNode('IMG');

  img.style.border=0;
  if (onload) {
    img.onload=onload;
  }
  if (_cs_isIE) {
    img.galleryImg = false;
  }
  img.src = src;

  return img;
}

function csSinglePixelIMG(colour) {
  var imgfile = colour+"-1x1.png";
  if (_cs_singlePixelIMGPrefix) {
    imgfile = _cs_singlePixelIMGPrefix + imgfile;
  }
  return csIMG(imgfile);
}

function csBoxInView(viewport, box) {
  box = csStringable({x: box.x, y:box.y, width: box.width, height: box.height});

  if (box.x + box.width > viewport.x + viewport.width)
    box.x = viewport.x + viewport.wdith - box.width;
  if (box.x < 0)
    box.x = 0;

  if (box.y + box.height > viewport.y + viewport.height)
    box.y = viewport.y + viewport.wdith - box.height;
  if (box.y < 0)
    box.y = 0;

  return box;
}

function csScreenXYtoDocXY(xy) {
  var docxy = csStringable({x: xy.x, y: xy.y});

  if (_cs_isIE) {
    docxy.x -= window.screenLeft;
    docxy.x += document.body.scrollLeft;
    docxy.y -= window.screenTop;
    docxy.y += document.body.scrollTop;
  } else {
    // BUG: doesn't account for the toolbars
    docxy.x -= window.screenX;
    docxy.x += window.pageXOffset;
    docxy.y -= window.screenY;
    docxy.y += window.pageYOffset;
  }

  return docxy;
}

// Return the bounding box of the viewport in doc coordinates.
function csViewPort() {
  var xy = csStringable({});
  if (_cs_isIE) {
    xy.x=document.body.scrollLeft;
    xy.y=document.body.scrollTop;
    xy.width=document.body.offsetWidth-24; // hack!
    xy.height=document.body.offsetHeight;
  } else {
    xy.x=window.pageXOffset;
    xy.y=window.pageYOffset;
    xy.width=window.innerWidth;
    xy.height=window.innerHeight;
  }

  return xy;
}

function csAbsTopLeft(elem) {
  var topLeft = csXY(elem.offsetLeft, elem.offsetTop);
  var parent=elem.offsetParent;

  while (parent && parent != elem) {
    elem=parent;
    topLeft.x += elem.offsetLeft;
    topLeft.y += elem.offsetTop;
    parent=elem.offsetParent;
  }

  return topLeft;
}

function csElementToDocBBox(elem) {
  var abs = csAbsTopLeft(elem);
  return csStringable({x: abs.x, y: abs.y, width: elem.offsetWidth, height: elem.offsetHeight});
}

function csMkLogWindow(width, height) {
  if (width == null) width="50%";
  if (height == null) height="15%";

  var div = csDIV();
  document.body.appendChild(div);
  div.style.overflow="auto";
  div.style.borderWidth=1;
  csSetSize(div,width,height);
  csSetPosition(div,csXY("50%","0%"));
  csSetZIndex(div,1023);
  _logTo(div);
  return div;
}
csMkLogWindow();

function csSetZIndex(elem,z) {
  elem.style.zIndex = z;
}

function csSetPosition(elem,xy) {
  elem.style.position='absolute';
  elem.style.left=xy.x;
  elem.style.top=xy.y;
}

function csSetRPosition(elem,dxy) {
  csSetPosition(elem, csXY(elem.offsetLeft + dxy.x, elem.offsetTop + dxy.y));
}

// Set size of one element to the size of another.
function csSetSizeFrom(elem, oElem) {
  csSetSize(elem, csXY(oElem.offsetWidth, oElem.offsetHeight));
}

function csSetSize(elem,width,height) {
  elem.style.width = width;
  elem.style.height = height;
}

function csClientMapAddHotSpot(map,hot) {
  var a = csNode("AREA");
  a.title=hot.title;
  a.alt=hot.title;
  a.href=hot.href;
  a.shape="RECT";
  a.coords=hot.x+','+hot.y+','+(hot.x+hot.dx)+','+(hot.y+hot.dy);
  map.appendChild(a);
}

function csHotspotsToClientMap(mapname,hotspots) {
  var map = csNode("MAP");
  map.name=mapname;
  _log("new MAP: name="+map.name);

  var hslen=hotspots.length;
  _log("hs2a: "+hslen+" hotspots");
  var h;
  for (var i=0; i<hslen; i++) {
    h=hotspots[i];
    if (h) csClientMapAddHotSpot(h);
  }

  return map;
}

// Create a new DIV using a meta object, with the specified corners and
// optional z-index (default 1).
// Meta: .onclick, function to be called with (e, CSHotSpot).
//       .href, if no .onclick, URL to open if clicked
//       .getHoverDiv, function to be called on mouseover which
//                     creates a DIV to show until mouseout
//       .title, if no .getHoverDiv, a title/alt string for the hotspot
//
// Return: object with .meta, the meta object
//                     .element, the DIV
//
function CSHotSpot(meta,xy1,xy2,z) {
  _log("CSHotSpot(xy1="+xy1+", xy2="+xy2+")");
  var me = this;

  if (z == null) z=1;

  this.xy1=xy1;
  this.xy2=xy2;

  var hotdiv = csDIV();
  _log("cssp1");
  csSetPosition(hotdiv,xy1);
  _log("set hot size: "+(xy2.x-xy1.x)+"x"+(xy2.y-xy1.y));
  csSetSize(hotdiv,xy2.x-xy1.x,xy2.y-xy1.y);
  csSetZIndex(hotdiv,z);
  //hotdiv.style.opacity=0.5;

  hotdiv.style.overflow="hidden";

  //label = csText(label);
  //hotdiv.appendChild(label);

  if (0) {
    var img = document.createElement("IMG");
    img.src="http://docs.python.org/icons/contents.png";
    csSetPosition(img,0,0);
    csSetSize(img,xy2.x-xy1.x,xy2.y-xy1.y);
    hotdiv.appendChild(img);
  }

  if (meta.onclick || meta.href) {
    hotdiv.onclick = function(e) {
        if (meta.onclick) {
          _log("calling "+meta.onclick);
          meta.onclick(e,me);
        } else if (meta.href) {
          document.location=meta.href;
        } else {
          _log("BUG: no onclick or href in meta");
        }
      }
  }

  if (meta.getHoverDiv) {
    hotdiv.onmouseover = function(e) {
        if (!e) e=window.event;
        var popup=me.getHoverDiv(e.screenX, e.screenY);
        if (popup) {
          popup.style.display='block';
        }
      }

    hotdiv.onmouseout = function(e) {
        if (!e) e=window.event;
        var popup=me.getHoverDiv(e.screenX, e.screenY);
        if (popup) {
          popup.style.display='none';
        }
      }
  } else if (meta.title) {
    hotdiv.title=meta.title;
    if (_cs_isIE) hotdiv.alt=meta.title;
  }

  this.meta=meta;
  this.element=hotdiv;
}

CSHotSpot.prototype.getHoverDiv = function(mouseScreenX, mouseScreenY) {

  if (!this.hoverDiv) {
    if (this.meta.getHoverDiv) {
      var hover=this.meta.getHoverDiv();
      hover.style.display='none';
      document.body.appendChild(hover);
      this.hoverDiv = hover;
    }
  }

  if (this.hoverDiv) {
    // position the popup just below the hotspot
    // we do this every time because the hotspot may have moved
    var up  = this.element.parentNode;
    var hotxy = csAbsTopLeft(up);
    var pos = csXY(hotxy.x + this.xy1.x, hotxy.y + this.xy2.y);
    //var pos = csScreenXYtoDocXY(csXY(mouseScreenX, mouseScreenY));

    if (false && pos.x + this.hoverDiv.offsetWidth > up.offsetWidth)
      pos.x = up.offsetWidth - this.hoverDiv.offsetWidth;
    if (false && pos.y + this.hoverDiv.offsetHeight > up.offsetHeight) {
      _log("UP.HEIGHT = "+up.offsetHeight+", up = "+up);
      pos.y = up.offsetHeight - this.hoverDiv.offsetHeight;
    }
    csSetPosition(this.hoverDiv, pos);
  }

  return this.hoverDiv;
}

_cs_anims=[];
_cs_nanim=0;
function runanim(id, fn) {
  var delay = fn();
  if (delay > 0) {
    setTimeout(function(){runanim(id,fn)}, delay);
  }
}

///////////////////////////////////////////////////////////////////
// CGI-based RPC infrastructure
//
_cs_rpc=csNode("DIV");
_cs_rpc_script=csNode("SCRIPT");
_cs_rpc.style.display='none';
_cs_rpc.appendChild(_cs_rpc_script);
document.body.appendChild(_cs_rpc);
_cs_rpc_callbacks={};
_cs_rpc_max=2
_cs_rpc_running=0
_cs_rpc_queue=[]

function csRPC(jscgiurl,argobj,callback,priority) {
  // Queue requests if too busy.
  if (_cs_rpc_max > 0 && _cs_rpc_running >= _cs_rpc_max) {
    _log("RPC QUEUE "+jscgiurl);
    if (priority) {
      _cs_rpc_queue.splice(0,0,[jscgiurl,argobj,callback]);
    } else {
      _cs_rpc_queue.push([jscgiurl,argobj,callback]);
    }
    return;
  }

  var seq = csSeq();
  var cbk = seq+"";
  _cs_rpc_callbacks[cbk]=callback;
  jscgiurl+="/"+seq;
  if (argobj) {
    csStringable(argobj);
    jscgiurl+="/"+argobj;
  }

  var rpc = csNode("SCRIPT");
  // BUG: possibly, replacing a SCRIPT may abort the script load
  _cs_rpc.replaceChild(rpc,_cs_rpc_script);
  _cs_rpc_script=rpc;

  _log("RPC DISPATCH "+jscgiurl);
  rpc.src=jscgiurl;
  _cs_rpc_running++;
}

function csRPC_doCallback(seq,result) {
  _log("RPC RETURN SEQ = "+seq);
  var cbk = seq+"";
  var cb = _cs_rpc_callbacks[cbk];
  delete _cs_rpc_callbacks[cbk];

  // Dequeue pending requests up to the limit.
  _cs_rpc_running--;
  while ( (_cs_rpc_max == 0 || _cs_rpc_running < _cs_rpc_max)
       && _cs_rpc_queue.length > 0
        ) {
    var dq = _cs_rpc_queue.shift();
    csRPC(dq[0],dq[1],dq[2]);
  }

  cb(result);
}

function csRPCbg(jscgiurl,argobj,callback) {
  setTimeout(function(){ csRPC(jscgiurl,argobj,callback); }, 0);
}

function csSubClass(baseClass, constructor) {
  _log("typeof constructor="+(typeof constructor));
  for (var k in baseClass.prototype) {
    eval("constructor.prototype."+k+"=baseClass.prototype."+k+";");
  }
  return constructor;
}

//////////////////////////////////////////////////
// An object with asyncchronous attribute methods.
//
function csAsyncObject() {
  this.asyncAttrs={};
}
csAsyncObject.prototype.addAttr = function(attrname, rpcurl, rpcargs) {
  if (!rpcurl) rpcurl="rpc.cgi";
  if (!rpcargs) rpcargs={rpc: attrname, key: this.key};
  if (!this.asyncAttrs) this.asyncAttrs={};

  var attrs = this.asyncAttrs[attrname] = {};
  attrs.rpc = [rpcurl,rpcargs];
};
csAsyncObject.prototype.withAttr = function(attrname, callback) {
  var me = this;
  var attr = me.asyncAttrs[attrname];
  if (!attr) _log("me.asyncAttrs["+attrname+"]="+attr);

  if (attr.value) {
    callback(attr.value);
  } else if (attr.callbacks) {
    attr.callbacks.push(callback);
  } else {
    attr.callbacks=[callback];
    csRPC(attr.rpc[0], attr.rpc[1], function(res) { me.setAttr(attrname, res); });
  }
};
csAsyncObject.prototype.setAttr = function(attrname, value) {
  var attr = this.asyncAttrs[attrname];
  attr.value = value;
  var callbacks = attr.callbacks;
  if (callbacks) {
    delete attr.callbacks;
    for (var i=0; i<callbacks.length; i++) {
      callbacks[i](value);
    }
  }
};

function csAsyncClass(constructor) {
  if (!constructor) constructor = function(key){ this.key=key; };
  return csSubClass(csAsyncObject, constructor);
}

_csPan_useImageMap=false;
_csPan_dragCursor=null;
_csPan_draggingCursor=null;
if (_cs_isGecko) {
  _csPan_dragCursor="-moz-grab";
  _csPan_draggingCursor="-moz-grabbing";
}
if (_cs_isIE) {
  _csPan_useImageMap=true;
}

/**
 * Controls a DIV containing the specified element with a mouse handler
 * to pan it.
 */
function CSPan(toPan) {
  _log("new pan div, toPan = "+toPan);

  var outer = csDIV();
  outer.style.position='relative';
  outer.style.overflow='hidden';

  outer.appendChild(toPan);
  csSetPosition(toPan, csXY(0,0));
  csSetZIndex(toPan,0);

  var glass = null;
  var hotLayer = null;
  var map = null;
  var mapName = null;

  if (_csPan_useImageMap) {
    glass = null;
    hotLayer = null;
    mapName="_csPan_map"+csSeq();
    map = csHotspotsToClientMap(mapName,[]);
    outer.appendChild(map);
    document.body.appendChild(map);
  } else {
    // Place some glass over the object to prevent drag'n'drop causing
    // trouble. Make it full size to cover the outer DIV.
    var glass = csDIV();
    outer.appendChild(glass);
    csSetPosition(glass, csXY(0,0));
    csSetSize(glass,"100%","100%");
    csSetZIndex(glass,1);

    // Place another layer over the glass for hotspots.
    // We pan this layer with the toPan object to keep the hotspots aligned.
    hotLayer = csDIV();
    outer.appendChild(hotLayer)
    csSetPosition(hotLayer, csXY(0,0));
    csSetZIndex(hotLayer,2);
  }

  var me = this;
  this.onMouseDown = function(e) { me.handleDown(e); };
  this.onMouseMove = function(e) { me.handleMove(e); };
  this.onMouseUp   = function(e) { me.handleUp(e); };
  this.onKeyPress  = function(e) { _log("K"); me.handleKeyPress(e); };

  var mouseElem = (glass ? glass : toPan);
  mouseElem.onmousedown = this.onMouseDown;
  if (_csPan_dragCursor) mouseElem.style.cursor=_csPan_dragCursor;

  var keyElem = hotLayer;       //(glass ? glass : outer);
  keyElem.onkeypress = this.onKeyPress;

  this.element=outer;
  this.glass=glass;
  this.map=map;
  this.mapName=mapName;
  this.hotLayer=hotLayer;
  this.toPan=toPan;
}

CSPan.prototype.addHotSpot = function(meta,z) {

  csStringable(meta);
  xy1=csXY(meta.x, meta.y);
  xy2=csXY(meta.x+meta.dx, meta.y+meta.dy);
  _log("xy1="+xy1+", xy2="+xy2);

  var hot = new CSHotSpot(meta, xy1, xy2, z);

  if (this.map) {
    csClientMapAddHotSpot(this.map,meta);
  }

  if (this.hotLayer) {
    var div = hot.element;
    _log("add hot div: "+div+" width="+(xy2.x-xy1.x));
    csSetPosition(div,xy1);
    csSetSize(div, xy2.x-xy1.x, xy2.y-xy1.y);
    if (z != null) csSetZIndex(div, z);
    this.hotLayer.appendChild(div);
  }

  return hot;
}

CSPan.prototype.addHotSpots = function(hotspots,z) {
  var spot;
  for (var i=0; i < hotspots.length; i++) {
    spot=hotspots[i];
    if (spot) {
      this.addHotSpot(spot,z);
    }
  }
};

// Set centre point from fraction.
CSPan.prototype.setCentre = function(fxy) {
  var newTop = this.element.offsetHeight/2 - fxy.y * this.toPan.offsetHeight;
  var newLeft = this.element.offsetWidth/2 - fxy.x * this.toPan.offsetWidth;
  this.setPosition(csXY(newLeft, newTop));
  this.centreFXY = fxy;
}

// Return centre point as fraction.
CSPan.prototype.getCentre = function() {
  var width = this.toPan.offsetWidth;
  var height = this.toPan.offsetHeight;
  if (width == 0 || height == 0) return null;
  return csXY( (this.element.offsetWidth/2-this.toPan.offsetLeft) / width,
               (this.element.offsetHeight/2-this.toPan.offsetTop) / height);
}

CSPan.prototype.reCentre = function() {
  if (! this.centreFXY) {
    this.centreFXY=this.getCentre();
    if (this.centreFXY == null) {
      _log("no centre yet, no reCentre");
      return;
    }
  }
  this.setCentre(this.centreFXY);
}

CSPan.prototype.setPosition = function(topLeft) {
  if (topLeft.x > 0) topLeft.x = 0;
  if (topLeft.y > 0) topLeft.y = 0;
  csSetPosition(this.toPan, topLeft);
  if (this.hotLayer) { csSetPosition(this.hotLayer, topLeft); }
  this.centreFXY = null;
}

CSPan.prototype.setSize = function(width, height) {
  var me = this;
  var cxy = me.getCentre();
  _log("pan.setSize(width="+width+",height="+height+")");
  csSetSize(me.element, width, height);
  me.reCentre();
};

CSPan.prototype.handleKeyPress = function(e) {
  if (!e) e=window.event;

  var keycode = (_is_IE ? e.keyCode : e.which);
  _log("keycode = "+keycode);

  return false;
};

CSPan.prototype.handleDown = function(e) {
  if (!e) e=window.event;

  this.panning=true;
  this.mouseXY = csXY(e.clientX, e.clientY);
  this.toPanTopLeft = csXY(this.toPan.offsetLeft, this.toPan.offsetTop);

  if (document.addEventListener) {
    document.addEventListener("mousemove", this.onMouseMove,true);
    document.addEventListener("mouseup",   this.onMouseUp,  true);

    if (_csPan_draggingCursor) {
      this.savedDocCursor = document.body.style.cursor;
      document.body.style.cursor = _csPan_draggingCursor;
      if (this.glass) this.glass.style.cursor = _csPan_draggingCursor;
    }
  } else {
    this.toPan.attachEvent("onmousemove", this.onMouseMove);
    this.toPan.attachEvent("onmouseup",   this.onMouseUp);
    this.toPan.setCapture();

    if (_csPan_draggingCursor) {
      this.savedPanCursor = this.toPan.style.cursor;
      this.toPan.style.cursor = _csPan_draggingCursor;
    }
  }

  if (this.hotLayer) this.hotLayer.style.display='none';
};

CSPan.prototype.handleMove = function(e) {
  //_log("move");
  if (!e) e=window.event;

  if (this.panning) {
    // current mouse position
    var newMouseXY = csXY( e.clientX, e.clientY);
    // offset from original mouse down position
    var dxy = csXY( newMouseXY.x - this.mouseXY.x,
                    newMouseXY.y - this.mouseXY.y );
    // new toPan div top left
    var newTopLeft = csXY( this.toPanTopLeft.x + dxy.x, this.toPanTopLeft.y + dxy.y );

    // Don't left the pan get away.
    if (newTopLeft.x+this.toPan.offsetWidth < this.element.offsetWidth) {
      newTopLeft.x=this.element.offsetWidth-this.toPan.offsetWidth;
    }
    if (newTopLeft.x > 0) newTopLeft.x=Math.max(0, this.toPanTopLeft.x);

    if (newTopLeft.y+this.toPan.offsetHeight < this.element.offsetHeight) {
      newTopLeft.y=this.element.offsetHeight-this.toPan.offsetHeight;
    }
    if (newTopLeft.y > 0) newTopLeft.y=Math.max(0, this.toPanTopLeft.y);

    this.setPosition(newTopLeft);
  }
};

CSPan.prototype.handleUp = function(e) {
  if (!e) e=window.event;

  if (this.panning) {
    this.panning=false;
    if (this.glass && _csPan_dragCursor)
      this.glass.style.cursor = _csPan_dragCursor;

    if (document.removeEventListener) {
      document.removeEventListener("mousemove", this.onMouseMove, true);
      document.removeEventListener("mouseup",   this.onMouseUp, true);
      if (_csPan_draggingCursor) {
        document.body.style.cursor = this.savedDocCursor;
        this.savedDocCursor = null;
      }
    } else {
      this.toPan.detachEvent("onmousemove", this.onMouseMove);
      this.toPan.detachEvent("onmouseup", this.onMouseMove);
      this.toPan.releaseCapture();
      if (_csPan_draggingCursor) {
        this.toPan.style.cursor = this.savedPanCursor;
        this.savedPanCursor = null;
      }
    }

    if (this.hotLayer) this.hotLayer.style.display='block';
  }
};

{ var vp = csViewPort();
  _log("viewport = "+vp.x+"x"+vp.y+", "+vp.width+"x"+vp.height);
}
