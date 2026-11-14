/* StudentPlanner 3D background.

   Floating study items (books, pencils, rulers, set squares, a protractor, a compass,
   an eraser and notebooks) drift as you scroll, lean toward the mouse, open or spin when touched, and burst in
   after you log in.

   Plain WebGL, no libraries, works offline. If WebGL is missing, the user prefers
   reduced motion, or the "Effects" button is switched off, the page simply looks
   flat and everything else keeps working. */
(function () {
  "use strict";
  var root = document.documentElement;
  var FX_KEY = "studplan.fx";
  var reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- tiny math helpers (column-major 4x4 matrices) ---------- */
  function ident() { return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]); }
  function mul(a, b) {
    var o = new Float32Array(16);
    for (var c = 0; c < 4; c++) for (var r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
    return o;
  }
  function translate(x, y, z) { var m = ident(); m[12] = x; m[13] = y; m[14] = z; return m; }
  function scaleM(s) { var m = ident(); m[0] = s; m[5] = s; m[10] = s; return m; }
  function rotX(a) { var c = Math.cos(a), s = Math.sin(a), m = ident(); m[5] = c; m[6] = s; m[9] = -s; m[10] = c; return m; }
  function rotY(a) { var c = Math.cos(a), s = Math.sin(a), m = ident(); m[0] = c; m[2] = -s; m[8] = s; m[10] = c; return m; }
  function rotZ(a) { var c = Math.cos(a), s = Math.sin(a), m = ident(); m[0] = c; m[1] = s; m[4] = -s; m[5] = c; return m; }
  function perspective(fov, aspect, near, far) {
    var f = 1 / Math.tan(fov / 2), m = new Float32Array(16);
    m[0] = f / aspect; m[5] = f; m[10] = (far + near) / (near - far); m[11] = -1; m[14] = 2 * far * near / (near - far);
    return m;
  }
  function normal3(m) { return new Float32Array([m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9], m[10]]); }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function easeOutBack(t) { var c1 = 1.70158, c3 = c1 + 1; t = clamp(t, 0, 1); return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); }
  function easeOutCubic(t) { t = clamp(t, 0, 1); return 1 - Math.pow(1 - t, 3); }
  function rng(seed) {
    return function () {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  /* ---------- geometry: study items made of boxes, cylinders and prisms ---------- */
  function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function unit(v) { var l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }
  function xf(m, p) {
    return [m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12], m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13], m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14]];
  }
  /* A part is a convex solid: a list of triangles and one colour. Parts are merged into one
     flat-shaded mesh with a colour per vertex. Normals are turned to face away from the
     part's centre, so the order the corners are listed in does not matter. */
  function part(tris, col, m) {
    m = m || ident();
    return { tris: tris.map(function (t) { return [xf(m, t[0]), xf(m, t[1]), xf(m, t[2])]; }), col: col };
  }
  function build(parts) {
    var n = 0; parts.forEach(function (p) { n += p.tris.length; });
    var pos = new Float32Array(n * 9), nor = new Float32Array(n * 9), col = new Float32Array(n * 9), i = 0;
    parts.forEach(function (p) {
      var c = [0, 0, 0], k = 0;
      p.tris.forEach(function (t) { t.forEach(function (v) { c[0] += v[0]; c[1] += v[1]; c[2] += v[2]; k++; }); });
      c = [c[0] / k, c[1] / k, c[2] / k];
      p.tris.forEach(function (t) {
        var nn = unit(cross(sub(t[1], t[0]), sub(t[2], t[0])));
        var mid = [(t[0][0] + t[1][0] + t[2][0]) / 3, (t[0][1] + t[1][1] + t[2][1]) / 3, (t[0][2] + t[1][2] + t[2][2]) / 3];
        var out = sub(mid, c);
        if (nn[0] * out[0] + nn[1] * out[1] + nn[2] * out[2] < 0) nn = [-nn[0], -nn[1], -nn[2]];
        for (var v = 0; v < 3; v++) { pos.set(t[v], i * 9 + v * 3); nor.set(nn, i * 9 + v * 3); col.set(p.col, i * 9 + v * 3); }
        i++;
      });
    });
    return { pos: pos, nor: nor, col: col, count: n * 3 };
  }
  function boxP(w, h, d, col, m) {
    var x = w / 2, y = h / 2, z = d / 2, v = [];
    for (var i = 0; i < 8; i++) v.push([i & 1 ? x : -x, i & 2 ? y : -y, i & 4 ? z : -z]);
    var q = [[0, 2, 3, 1], [4, 5, 7, 6], [0, 1, 5, 4], [2, 6, 7, 3], [0, 4, 6, 2], [1, 3, 7, 5]], tris = [];
    q.forEach(function (f) { tris.push([v[f[0]], v[f[1]], v[f[2]]], [v[f[0]], v[f[2]], v[f[3]]]); });
    return part(tris, col, m);
  }
  function prismP(poly, thick, col, m) {            // convex polygon [x, z] points, thickness along y
    var h = thick / 2, tris = [], n = poly.length;
    for (var i = 1; i < n - 1; i++) {
      tris.push([[poly[0][0], h, poly[0][1]], [poly[i][0], h, poly[i][1]], [poly[i + 1][0], h, poly[i + 1][1]]]);
      tris.push([[poly[0][0], -h, poly[0][1]], [poly[i][0], -h, poly[i][1]], [poly[i + 1][0], -h, poly[i + 1][1]]]);
    }
    for (var j = 0; j < n; j++) {
      var a = poly[j], b = poly[(j + 1) % n];
      tris.push([[a[0], h, a[1]], [b[0], h, b[1]], [b[0], -h, b[1]]], [[a[0], h, a[1]], [b[0], -h, b[1]], [a[0], -h, a[1]]]);
    }
    return part(tris, col, m);
  }
  function cylP(r, h, seg, col, m) {                // along y
    var poly = [];
    for (var i = 0; i < seg; i++) poly.push([r * Math.cos(i / seg * Math.PI * 2), r * Math.sin(i / seg * Math.PI * 2)]);
    return prismP(poly, h, col, m);
  }
  function coneP(r, h, seg, col, m) {               // base at -h/2, point at +h/2
    var tris = [], hh = h / 2;
    for (var i = 0; i < seg; i++) {
      var a = i / seg * Math.PI * 2, b = (i + 1) / seg * Math.PI * 2;
      var p1 = [r * Math.cos(a), -hh, r * Math.sin(a)], p2 = [r * Math.cos(b), -hh, r * Math.sin(b)];
      tris.push([p1, p2, [0, hh, 0]], [p1, p2, [0, -hh, 0]]);
    }
    return part(tris, col, m);
  }
  function T(x, y, z) { return translate(x, y, z); }
  function R(rx, ry, rz) { return mul(rotY(ry || 0), mul(rotX(rx || 0), rotZ(rz || 0))); }
  var C = {
    cream: [0.97, 0.94, 0.84], wood: [0.86, 0.67, 0.43], lead: [0.18, 0.18, 0.2], yellow: [0.98, 0.78, 0.14], pink: [0.96, 0.56, 0.6],
    silver: [0.74, 0.77, 0.8], tick: [0.18, 0.22, 0.28], red: [0.82, 0.22, 0.2], blue: [0.2, 0.42, 0.78], green: [0.2, 0.56, 0.4],
    orange: [0.95, 0.52, 0.16], purple: [0.52, 0.38, 0.78], teal: [0.28, 0.74, 0.72], sky: [0.56, 0.79, 0.93], ruler: [0.99, 0.85, 0.4],
    white: [0.97, 0.97, 0.98], paper: [0.99, 0.98, 0.93]
  };
  // books and notebooks are two meshes: the body and a lid that swings open on a hinge
  function book(cover) {
    return { g: build([boxP(1.5, 0.05, 1.1, cover, T(0, -0.12, 0)), boxP(0.07, 0.29, 1.1, cover, T(-0.715, 0, 0)),
      boxP(1.4, 0.19, 1.02, C.cream, T(0.03, 0, 0)), boxP(0.05, 0.02, 0.4, C.red, T(0.4, 0.1, 0.7))]),
      lid: build([boxP(1.5, 0.05, 1.1, cover, T(0.74, 0, 0))]), hinge: [-0.74, 0.12, 0] };
  }
  function pencil(body) {
    var al = R(0, 0, -Math.PI / 2), parts = [cylP(0.09, 1.7, 6, body, mul(T(-0.1, 0, 0), al)), coneP(0.09, 0.5, 6, C.wood, mul(T(0.99, 0, 0), al)),
      coneP(0.032, 0.17, 6, C.lead, mul(T(1.3, 0, 0), al)), cylP(0.093, 0.16, 8, C.silver, mul(T(-1.03, 0, 0), al)), cylP(0.09, 0.2, 8, C.pink, mul(T(-1.2, 0, 0), al))];
    return build(parts);
  }
  function ruler(color) {
    var parts = [boxP(2.6, 0.04, 0.4, color)];
    for (var i = 0; i <= 20; i++) {
      var d = i % 5 === 0 ? 0.16 : 0.08;
      parts.push(boxP(0.014, 0.05, d, C.tick, T(-1.2 + i * 0.12, 0.002, 0.2 - d / 2)));
    }
    return build(parts);
  }
  function setSquare(color) {
    return build([prismP([[-0.8, -0.6], [0.8, -0.6], [-0.8, 0.85]], 0.05, color), prismP([[-0.5, -0.4], [0.28, -0.4], [-0.5, 0.36]], 0.054, C.paper)]);
  }
  function protractor() {
    var arc = [], inner = [], parts, i;
    for (i = 0; i <= 24; i++) { var a = i / 24 * Math.PI; arc.push([Math.cos(a), -Math.sin(a) + 0.5]); inner.push([0.6 * Math.cos(a), -0.6 * Math.sin(a) + 0.5]); }
    parts = [prismP(arc, 0.045, C.sky), prismP(inner, 0.049, C.paper)];
    for (i = 0; i <= 12; i++) { var b = i / 12 * Math.PI; parts.push(boxP(0.1, 0.05, 0.014, C.tick, mul(T(0.9 * Math.cos(b), 0.002, -0.9 * Math.sin(b) + 0.5), R(0, b, 0)))); }
    return build(parts);
  }
  function compass() {
    var parts = [cylP(0.08, 0.14, 10, C.silver, R(Math.PI / 2, 0, 0)), cylP(0.03, 0.4, 8, C.silver, T(0, 0.27, 0))], s;
    [-1, 1].forEach(function (s) {
      var a = s * 0.27, leg = mul(R(0, 0, a), T(0, -0.78, 0));
      parts.push(boxP(0.06, 1.56, 0.04, C.silver, leg));
      parts.push(coneP(0.03, 0.26, 6, s < 0 ? C.tick : C.lead, mul(R(0, 0, a), mul(T(0, -1.69, 0), R(0, 0, Math.PI)))));
    });
    return build(parts);
  }
  function eraser() { return build([boxP(0.9, 0.3, 0.45, C.white), boxP(0.46, 0.32, 0.47, C.blue, T(0.05, 0, 0))]); }
  function notebook(cover) {
    var parts = [boxP(1.2, 0.05, 1.6, cover, T(0, -0.07, 0)), boxP(1.1, 0.1, 1.5, C.paper, T(0.04, 0, 0))];
    for (var i = 0; i < 7; i++) parts.push(cylP(0.045, 0.24, 8, C.silver, mul(T(-0.6, 0, -0.66 + i * 0.22), R(0, 0, -Math.PI / 2))));
    return { g: build(parts), lid: build([boxP(1.2, 0.05, 1.6, cover, T(0.6, 0, 0))]), hinge: [-0.6, 0.09, 0] };
  }
  var KINDS = ["book", "pencil", "ruler", "square", "book", "protractor", "notebook", "compass", "pencil", "eraser", "ruler", "book", "square", "notebook", "book"];
  function itemGeometries() {
    var list = [book(C.red), pencil(C.yellow), ruler(C.ruler), setSquare(C.teal), book(C.blue), protractor(), notebook(C.purple), compass(),
      pencil(C.blue), eraser(), ruler(C.sky), book(C.green), setSquare(C.orange), notebook(C.teal), book(C.orange)];
    return list.map(function (it, i) { return it.g ? it : { g: it, lid: null, hinge: null }; }).map(function (it, i) { it.kind = KINDS[i]; return it; });
  }

  /* ---------- colours from the page's CSS tokens ---------- */
  function parseColor(v, fallback) {
    v = (v || "").trim();
    var m;
    if ((m = /^#([0-9a-f]{3})$/i.exec(v))) return [parseInt(m[1][0] + m[1][0], 16) / 255, parseInt(m[1][1] + m[1][1], 16) / 255, parseInt(m[1][2] + m[1][2], 16) / 255];
    if ((m = /^#([0-9a-f]{6})/i.exec(v))) return [parseInt(m[1].slice(0, 2), 16) / 255, parseInt(m[1].slice(2, 4), 16) / 255, parseInt(m[1].slice(4, 6), 16) / 255];
    if ((m = /^rgba?\(([^)]+)\)/.exec(v))) { var p = m[1].split(/[ ,\/]+/).map(Number); return [p[0] / 255, p[1] / 255, p[2] / 255]; }
    return fallback;
  }
  var TOKENS = [["--accent", [0.05, 0.42, 0.54]], ["--spark", [0.91, 0.64, 0.09]], ["--good", [0.11, 0.48, 0.28]], ["--bad", [0.7, 0.16, 0.12]], ["--bg", [0.94, 0.96, 0.97]]];
  var palette = TOKENS.map(function (t) { return t[1].slice(); });       // current (eases toward target)
  var target = TOKENS.map(function (t) { return t[1].slice(); });
  function readTheme(snap) {
    var cs = getComputedStyle(root);
    TOKENS.forEach(function (t, i) { target[i] = parseColor(cs.getPropertyValue(t[0]), t[1]); if (snap) palette[i] = target[i].slice(); });
  }

  /* ---------- the scene ---------- */
  var canvas = null, gl = null, prog = null, L = {}, geos = [], shapes = [];
  var enabled = false, running = false, raf = 0, frames = 0;
  var tNow = 0, introStart = 0, introLen = 1500, introBoost = 0;
  var scrollY = 0, scrollSmooth = 0, scrollVel = 0, lastScrollY = 0;
  var mouseX = 0, mouseY = 0, mx = 0, my = 0;
  var dprCap = Math.min(window.devicePixelRatio || 1, 1.5), slowFrames = 0, avgDt = 16, stage = 0, shown = 1;
  var TRAVEL = 44, FOV = 50 * Math.PI / 180;

  var VS = "attribute vec3 aPos;attribute vec3 aNor;attribute vec3 aCol;uniform mat4 uModel;uniform mat4 uVP;uniform mat3 uNM;" +
    "varying vec3 vN;varying vec3 vW;varying vec3 vCol;" +
    "void main(){vec4 w=uModel*vec4(aPos,1.0);vW=w.xyz;vN=uNM*aNor;vCol=aCol;gl_Position=uVP*w;}";
  var FS = "#ifdef GL_FRAGMENT_PRECISION_HIGH\nprecision highp float;\n#else\nprecision mediump float;\n#endif\n" +
    "varying vec3 vN;varying vec3 vW;varying vec3 vCol;uniform vec3 uColor;uniform float uMode;uniform float uLit;" +
    "uniform vec3 uCam;uniform vec3 uFog;uniform vec2 uFogRange;uniform vec3 uRim;uniform float uSoft;" +
    "void main(){vec3 base=uMode>0.5?uColor:vCol;vec3 col=base;" +
    "if(uLit>0.5){vec3 n=normalize(vN);vec3 l=normalize(vec3(0.45,0.8,0.55));float d=max(dot(n,l),0.0);float hemi=n.y*0.5+0.5;" +
    "vec3 v=normalize(uCam-vW);float rim=pow(1.0-max(dot(n,v),0.0),2.5);col=base*(0.36+0.5*d+0.24*hemi)+uRim*rim*0.32;}" +
    "float f=clamp((length(uCam-vW)-uFogRange.x)/(uFogRange.y-uFogRange.x),0.0,1.0);col=mix(col,uFog,f*0.88);col=mix(col,uFog,uSoft);" +
    "gl_FragColor=vec4(col,1.0);}";

  function compile(type, src) {
    var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  function buffer(data) { var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW); return b; }
  function makeGeo(g) {
    return { pos: buffer(g.pos), nor: buffer(g.nor), col: buffer(g.col), count: g.count };
  }
  function makeItem(it) {
    var geo = makeGeo(it.g), rad = 0, i, h = it.hinge || [0, 0, 0];
    for (i = 0; i < it.g.pos.length; i += 3) rad = Math.max(rad, Math.hypot(it.g.pos[i], it.g.pos[i + 1], it.g.pos[i + 2]));
    if (it.lid) for (i = 0; i < it.lid.pos.length; i += 3) rad = Math.max(rad, Math.hypot(it.lid.pos[i] + h[0], it.lid.pos[i + 1] + h[1], it.lid.pos[i + 2] + h[2]));
    geo.rad = rad; geo.kind = it.kind; geo.hinge = it.hinge; geo.lid = it.lid ? makeGeo(it.lid) : null;
    return geo;
  }

  function setup() {
    canvas = document.createElement("canvas");
    canvas.id = "scene";
    canvas.setAttribute("aria-hidden", "true");
    canvas.style.cssText = "position:fixed;inset:0;width:100%;height:100%;z-index:-1;pointer-events:none;display:block";
    gl = canvas.getContext("webgl", { alpha: true, antialias: true, premultipliedAlpha: true, powerPreference: "low-power" });
    if (!gl) return false;
    prog = gl.createProgram();
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, VS)); gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    ["aPos", "aNor", "aCol"].forEach(function (n) { L[n] = gl.getAttribLocation(prog, n); });
    ["uModel", "uVP", "uNM", "uColor", "uMode", "uLit", "uCam", "uFog", "uFogRange", "uRim", "uSoft"].forEach(function (n) { L[n] = gl.getUniformLocation(prog, n); });
    gl.enable(gl.DEPTH_TEST);
    geos = itemGeometries().map(makeItem);
    layout();
    document.body.insertBefore(canvas, document.body.firstChild);
    return true;
  }

  function layout() {
    var r = rng(20261112), n = window.innerWidth < 700 ? 18 : 30, kinds = geos.length, order = [], i;
    for (i = 0; i < kinds; i++) order.push(i);
    for (i = kinds - 1; i > 0; i--) { var j = Math.floor(r() * (i + 1)), t = order[i]; order[i] = order[j]; order[j] = t; }
    shapes = [];
    for (i = 0; i < n; i++) {
      var side = i % 3 === 0 ? 0 : (i % 2 ? 1 : -1), z = -13 + r() * 12.5;
      var x = side === 0 ? (r() - 0.5) * 15 : side * (3.2 + r() * 8);
      shapes.push({
        g: order[i % kinds], x: x, y: 9 - (i / n) * (TRAVEL + 20) + (r() - 0.5) * 3, z: z,
        s: 1.0 + r() * 0.55 + (-z) * 0.05, rx: r() * 6, ry: r() * 6, rz: r() * 6,
        vx: (r() - 0.5) * 0.3, vy: (r() - 0.5) * 0.5, vz: (r() - 0.5) * 0.3, ph: r() * 6.28, delay: i / n,
        f: 0, ft: 0, op: 0, kick: -1, closeAt: 0, sx: 0, sy: 0, sr: 0, sw: 0
      });
    }
  }

  function resize() {
    if (!canvas) return;
    var w = Math.max(1, Math.floor(window.innerWidth * dprCap)), h = Math.max(1, Math.floor(window.innerHeight * dprCap));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, w, h);
  }

  function useGeo(g) {
    gl.bindBuffer(gl.ARRAY_BUFFER, g.pos); gl.enableVertexAttribArray(L.aPos); gl.vertexAttribPointer(L.aPos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, g.nor); gl.enableVertexAttribArray(L.aNor); gl.vertexAttribPointer(L.aNor, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, g.col); gl.enableVertexAttribArray(L.aCol); gl.vertexAttribPointer(L.aCol, 3, gl.FLOAT, false, 0, 0);
  }
  function setObject(m) {
    gl.uniformMatrix4fv(L.uModel, false, m); gl.uniformMatrix3fv(L.uNM, false, normal3(m));
  }

  function frame(now) {
    raf = 0;
    if (!running) return;
    var dt = tNow ? Math.min(now - tNow, 100) : 16; tNow = now;
    frames++;
    // adapt to slow computers: lower resolution first, then draw fewer shapes
    avgDt += (dt - avgDt) * 0.05;
    if (frames > 60 && avgDt > 34) {
      if (++slowFrames > 45) {
        slowFrames = 0;
        if (stage === 0) { stage = 1; dprCap = 1; resize(); } else if (stage === 1) { stage = 2; shown = 0.5; }
      }
    } else slowFrames = 0;

    var doc = root, maxScroll = Math.max(1, doc.scrollHeight - window.innerHeight);
    scrollY = window.pageYOffset || 0;
    var progress = clamp(scrollY / maxScroll, 0, 1);
    var targetP = reduced ? 0 : progress;
    scrollSmooth += (targetP - scrollSmooth) * 0.08;
    var vel = (scrollY - lastScrollY) / Math.max(1, window.innerHeight); lastScrollY = scrollY;
    scrollVel += (clamp(vel * 40, -3, 3) - scrollVel) * 0.12;
    mx += (mouseX - mx) * 0.05; my += (mouseY - my) * 0.05;
    for (var i = 0; i < palette.length; i++) for (var k = 0; k < 3; k++) palette[i][k] += (target[i][k] - palette[i][k]) * 0.1;

    var t = reduced ? 0 : now / 1000;
    var intro = clamp((now - introStart) / introLen, 0, 1);
    var introE = easeOutCubic(intro);
    var spinBoost = 1 + Math.abs(scrollVel) * 2.4 + (1 - introE) * 5;

    var aspect = canvas.width / canvas.height;
    var proj = perspective(FOV, aspect, 0.1, 120);
    var camY = -scrollSmooth * TRAVEL + Math.sin(t * 0.25) * 0.4, camX = mx * 0.9, camZ = 10 + (1 - introE) * 16;
    var yaw = -mx * 0.06 + (1 - introE) * 0.6, pitch = my * 0.04 + scrollVel * 0.012;
    var view = mul(mul(rotX(-pitch), rotY(-yaw)), translate(-camX, -camY, -camZ));
    var vp = mul(proj, view);

    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.uniform3fv(L.uFog, palette[4]);
    var rim = palette[0];
    gl.uniform3fv(L.uRim, rim);

    // floating study items
    gl.uniformMatrix4fv(L.uVP, false, vp);
    gl.uniform3f(L.uCam, camX, camY, camZ);
    gl.uniform2f(L.uFogRange, 14, 42);
    gl.uniform1f(L.uSoft, 0.1);           // a touch of haze so text on top stays easy to read
    gl.uniform1f(L.uMode, 0); gl.uniform1f(L.uLit, 1);
    var count = Math.ceil(shapes.length * shown), H = window.innerHeight, W = window.innerWidth;
    for (var s = 0; s < count; s++) {
      var o = shapes[s], g = geos[o.g];
      var k2 = reduced ? 1 : easeOutBack((intro * 1.6 - o.delay * 0.6) / 0.9);
      o.vis = k2 > 0.25;
      if (k2 <= 0.001) continue;
      if (o.ft && now > o.closeAt) { o.ft = 0; o.ot = 0; }             // a book left open closes by itself
      o.f += (o.ft - o.f) * Math.min(1, dt * 0.008);
      o.op += ((o.ft ? 1 : 0) - o.op) * Math.min(1, dt * 0.007);
      var calm = 1 - o.f;
      o.rx += o.vx * dt * 0.001 * spinBoost * calm; o.ry += o.vy * dt * 0.001 * spinBoost * calm; o.rz += o.vz * dt * 0.001 * spinBoost * calm;
      var bob = Math.sin(t * 0.6 + o.ph) * 0.35, hop = 0, pulse = 1, K = null, p = o.kick < 0 ? 1 : (now - o.kick) / 1100;
      if (p < 1) {                                                      // touched: spin, hop or wobble
        var a1 = easeOutCubic(p), ang = a1 * Math.PI * 2, hopK = Math.sin(clamp(p, 0, 1) * Math.PI);
        hop = hopK * 1.1; pulse = 1 + 0.22 * hopK;
        if (g.kind === "pencil") K = rotX(ang * 3);
        else if (g.kind === "ruler") K = rotX(ang * 2);
        else if (g.kind === "square") K = rotX(ang);
        else if (g.kind === "compass") K = rotY(ang * 2);
        else if (g.kind === "eraser") K = rotZ(ang * 2);
        else if (g.kind === "protractor") K = rotZ(Math.sin(p * Math.PI * 3) * 0.9 * (1 - p));
      }
      var rx = o.rx + (1.1 - o.rx) * o.f, ry = o.ry + (0.12 - o.ry) * o.f, rz = o.rz + (0.08 - o.rz) * o.f;
      if (!g.lid) { rx = o.rx; ry = o.ry; rz = o.rz; }
      var px = o.x, py = o.y + bob + hop, pz = o.z + (g.lid ? o.f * 2.5 : 0), sc = o.s * k2 * pulse * (1 + (g.lid ? o.f * 0.35 : 0));
      var rot = mul(mul(rotX(rx), rotY(ry)), rotZ(rz));
      var m = mul(mul(translate(px, py, pz), rot), mul(K || ident(), scaleM(sc)));
      useGeo(g); setObject(m);
      gl.drawArrays(gl.TRIANGLES, 0, g.count);
      if (g.lid) {
        var mh = mul(m, mul(translate(g.hinge[0], g.hinge[1], g.hinge[2]), rotZ(2.7 * easeOutCubic(o.op))));
        useGeo(g.lid); setObject(mh);
        gl.drawArrays(gl.TRIANGLES, 0, g.lid.count);
      }
      // remember where it is on screen so a tap can find it
      var cw = vp[3] * px + vp[7] * py + vp[11] * pz + vp[15];
      if (cw > 0.1) {
        o.sx = ((vp[0] * px + vp[4] * py + vp[8] * pz + vp[12]) / cw * 0.5 + 0.5) * W;
        o.sy = (0.5 - (vp[1] * px + vp[5] * py + vp[9] * pz + vp[13]) / cw * 0.5) * H;
        o.sr = g.rad * sc * 0.8 * (1 / Math.tan(FOV / 2)) / cw * H / 2; o.sw = cw;
      } else o.sw = 0;
    }

    if (reduced) { running = false; return; }                      // one still picture is enough
    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (!enabled || running) return;
    running = true; tNow = 0; lastScrollY = window.pageYOffset || 0;
    raf = requestAnimationFrame(frame);
  }
  function stop() { running = false; if (raf) cancelAnimationFrame(raf); raf = 0; }

  function setEnabled(on) {
    try { localStorage.setItem(FX_KEY, on ? "on" : "off"); } catch (e) { }
    if (on && !gl && !init()) return false;
    enabled = !!on;
    if (canvas) canvas.style.display = enabled ? "block" : "none";
    root.dataset.fx = enabled ? "on" : "off";
    if (enabled) { resize(); introStart = performance.now(); introLen = 1200; start(); } else stop();
    return enabled;
  }

  function welcome() {
    if (!enabled) return;
    introStart = performance.now(); introLen = 2600;
    if (reduced) return;
    start();
  }

  /* ---------- touching the items: books open, the rest spin, hop or wobble ---------- */
  function pick(x, y) {
    var best = null, n = Math.ceil(shapes.length * shown);
    for (var i = 0; i < n; i++) {
      var o = shapes[i];
      if (!o.vis || o.sw <= 0) continue;
      if (Math.hypot(x - o.sx, y - o.sy) <= o.sr && (!best || o.sw < best.sw)) best = o;
    }
    return best;
  }
  function onPage(el) {                  // true when the tap landed on empty page, not a card, button or field
    if (!el || el.nodeType !== 1) return false;
    for (var n = el; n && n !== document.body && n !== root; n = n.parentElement) {
      if (/^(A|BUTTON|INPUT|SELECT|TEXTAREA|LABEL|SUMMARY|OPTION|CANVAS)$/.test(n.tagName) || n.getAttribute("role") === "dialog") return false;
      var bg = getComputedStyle(n).backgroundColor;
      if (bg && bg !== "transparent" && !/^rgba\(.*,\s*0\)$/.test(bg)) return false;
    }
    return true;
  }
  function touch(o) {
    var g = geos[o.g], now = performance.now();
    if (g.lid) {
      if (o.ft) { o.ft = 0; return "close"; }
      o.rx = Math.atan2(Math.sin(o.rx), Math.cos(o.rx)); o.ry = Math.atan2(Math.sin(o.ry), Math.cos(o.ry)); o.rz = Math.atan2(Math.sin(o.rz), Math.cos(o.rz));
      o.ft = 1; o.closeAt = now + 7000; return "open";
    }
    o.kick = now; return "move";
  }
  function tap(x, y, force) {
    if (!enabled || reduced) return null;
    var o = pick(x, y);
    if (!o) return null;
    if (!force && !onPage(document.elementFromPoint(x, y))) return null;
    if (!running) start();
    return { kind: geos[o.g].kind, did: touch(o) };
  }

  function init() {
    try {
      if (!setup()) { root.dataset.fx = "none"; return false; }
    } catch (e) {
      console.warn("StudentPlanner 3D background is off:", e.message);
      root.dataset.fx = "none"; gl = null; return false;
    }
    readTheme(true);
    resize();
    window.addEventListener("resize", function () { resize(); if (reduced && enabled) { running = false; start(); } });
    document.addEventListener("click", function (e) { tap(e.clientX, e.clientY); });
    var hoverAt = 0, hovering = false;
    window.addEventListener("pointermove", function (e) {
      if (e.pointerType === "mouse" && enabled && !reduced && e.timeStamp - hoverAt > 40) {
        hoverAt = e.timeStamp;
        var over = !!pick(e.clientX, e.clientY) && onPage(e.target);
        if (over !== hovering) { hovering = over; root.style.cursor = over ? "pointer" : ""; }
      }
      mouseX = (e.clientX / window.innerWidth) * 2 - 1; mouseY = (e.clientY / window.innerHeight) * 2 - 1;
    }, { passive: true });
    document.addEventListener("visibilitychange", function () { if (document.hidden) stop(); else if (enabled) start(); });
    new MutationObserver(function () { readTheme(false); if (reduced && enabled) { readTheme(true); running = false; start(); } })
      .observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    if (window.matchMedia) {
      var mq = matchMedia("(prefers-color-scheme: dark)");
      (mq.addEventListener ? mq.addEventListener.bind(mq, "change") : mq.addListener.bind(mq))(function () { readTheme(false); });
    }
    canvas.addEventListener("webglcontextlost", function (e) { e.preventDefault(); stop(); root.dataset.fx = "none"; });
    return true;
  }

  var wanted = true;
  try { wanted = localStorage.getItem(FX_KEY) !== "off"; } catch (e) { }
  window.StudScene = {
    welcome: welcome,
    setEnabled: setEnabled,
    isEnabled: function () { return enabled; },
    available: function () { return !!gl; },
    tap: tap,
    items: function () { return shapes.map(function (o) { return { kind: geos[o.g].kind, x: o.sx, y: o.sy, r: o.sr, open: o.ft, kick: o.kick }; }); },
    stats: function () { return { frames: frames, shapes: shapes.length, dpr: dprCap }; }
  };
  function boot() {
    if (wanted) setEnabled(true); else root.dataset.fx = "off";
    root.dataset.fxReady = "1";
    document.dispatchEvent(new CustomEvent("studscene:ready"));
  }
  if (document.body) boot(); else document.addEventListener("DOMContentLoaded", boot);
})();
