/* The picked bus, marked the same way on every bus map: a small WebGL layer just under the buses that draws
   only the picked bus's light, animated every frame on its own (the bus layer redraws far less often).

   Zoomed out (the bus is a disc, or a small sprite): thin rings ripple out of it and fade, two at a time, over
   a soft halo, so it is found at once among the rest. They fade away from zoom 15 to 16.5.
   Zoomed in: no ring over the bus. Every few seconds the bus glows, and light leaves its outline, swells,
   rounds into a circle and fades. All of it is drawn under the buses, so nothing covers the picked one.

     const FX = SELFX.create(map, { id: 'live-gl-fx', tex: () => busLayer.tex });   // the bus layer's own texture
     FX.add('live-gl')          after the bus layer: goes just under it
     FX.set({ id, lng, lat, col: [r, g, b], rect: [u0, v0, u1, v1], bw, bh, lift, mk, discR, z })   or FX.set(null)
        rect: the bus sprite's cell in the atlas (uv); bw, bh, lift: how the bus layer draws that sprite (px);
        mk: 1 where the bus is a disc of radius discR (px), 0 where it is the sprite; z: map zoom
     FX.show(v)

   Picking, unpicking and every zoom change fade over a fraction of a second; with reduced motion asked for,
   the rings and glow are drawn still. */
'use strict';
const SELFX = (() => {
  const VS = `
    attribute vec2 a_pos; attribute vec2 a_off; attribute vec2 a_uv; attribute vec4 a_col; attribute float a_mode;
    uniform mat4 u_matrix; uniform vec2 u_view;
    varying vec2 v_uv; varying vec4 v_col; varying float v_mode;
    void main() {
      vec4 p = u_matrix * vec4(a_pos, 0.0, 1.0);
      p.xy += vec2(a_off.x, -a_off.y) * 2.0 / u_view * p.w;
      gl_Position = p; v_uv = a_uv; v_col = a_col; v_mode = a_mode;
    }`;
  /* mode 0: the bus's outline as soft light (its sprite's alpha, blurred, only inside its own atlas cell);
     mode 1: a soft round glow; mode 2 + w: a thin ring at 0.8 of the quad, w its width, with a faint bloom */
  const FS = `
    precision mediump float;
    uniform sampler2D u_tex; uniform vec4 u_rect; uniform vec2 u_tap;
    varying vec2 v_uv; varying vec4 v_col; varying float v_mode;
    float A(vec2 q) { vec2 i = step(u_rect.xy, q) * step(q, u_rect.zw); return texture2D(u_tex, q).a * i.x * i.y; }
    void main() {
      if (v_mode > 1.5) {
        float w = v_mode - 2.0, d = length(v_uv * 2.0 - 1.0), x = (d - 0.8) / w, y = (d - 0.8) / (w * 5.0);
        float a = (exp(-x * x) + 0.22 * exp(-y * y)) * (1.0 - smoothstep(0.94, 1.0, d));
        gl_FragColor = v_col * a; return;
      }
      if (v_mode > 0.5) {
        float d = length(v_uv * 2.0 - 1.0);
        gl_FragColor = v_col * exp(-d * d * 4.5) * (1.0 - smoothstep(0.9, 1.0, d)); return;
      }
      vec2 t = u_tap, u = vec2(t.x, -t.y);
      float a = A(v_uv) * 0.16
        + (A(v_uv + vec2(t.x, 0.0)) + A(v_uv - vec2(t.x, 0.0)) + A(v_uv + vec2(0.0, t.y)) + A(v_uv - vec2(0.0, t.y))) * 0.11
        + (A(v_uv + t) + A(v_uv - t) + A(v_uv + u) + A(v_uv - u)) * 0.07
        + (A(v_uv + 2.0 * vec2(t.x, 0.0)) + A(v_uv - 2.0 * vec2(t.x, 0.0)) + A(v_uv + 2.0 * vec2(0.0, t.y)) + A(v_uv - 2.0 * vec2(0.0, t.y))) * 0.04;
      gl_FragColor = v_col * min(1.0, a * 1.5);
    }`;
  const STRIDE = 11;                                           // pos2 off2 uv2 col4 mode1
  const MOBILE = matchMedia('(pointer: coarse)').matches || (navigator.hardwareConcurrency || 8) <= 4;
  const CALM = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const mercX = lng => (lng + 180) / 360;
  const mercY = lat => (180 - (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360))) / 360;
  const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
  const sstep = (a, b, v) => { const x = clamp((v - a) / (b - a)); return x * x * (3 - 2 * x); };
  const outCubic = p => 1 - Math.pow(1 - p, 3);
  const SQ = [0, 0, 1, 1];

  function create(map, opts) {
    const buf = new Float32Array(STRIDE * 6 * 16);
    let n = 0, cur = null, shown = null, t0 = 0, tOff = 0, on = true, rect = SQ, pending = false;
    function quad(w, h, uv, col, a, add, mode, dy = 0) {
      if (a <= 0.002 || (n + 6) * STRIDE > buf.length) return;
      const x0 = -w / 2, x1 = w / 2, y0 = -h / 2 + dy, y1 = h / 2 + dy;
      const P = [[x0, y0, uv[0], uv[1]], [x1, y0, uv[2], uv[1]], [x1, y1, uv[2], uv[3]], [x0, y0, uv[0], uv[1]], [x1, y1, uv[2], uv[3]], [x0, y1, uv[0], uv[3]]];
      for (const [ox, oy, u, v] of P) {
        const o = n++ * STRIDE;
        buf[o] = 0; buf[o + 1] = 0; buf[o + 2] = ox; buf[o + 3] = oy; buf[o + 4] = u; buf[o + 5] = v;
        // premultiplied; `add` lowers the alpha that darkens what is under it, so the light adds rather than covers
        buf[o + 6] = col[0] * a; buf[o + 7] = col[1] * a; buf[o + 8] = col[2] * a; buf[o + 9] = a * (1 - add); buf[o + 10] = mode;
      }
    }
    const ring = (r, px, col, a, dy) => { const H = r / 0.8; quad(2 * H, 2 * H, SQ, col, a, 0.25, 2 + clamp(px / H, 0.004, 0.9), dy); };
    // the bus's outline, k times its size, padded so the blur and the swell have room inside the quad
    const outline = (S, k, col, a) => {
      const pad = 0.35, [u0, v0, u1, v1] = rect, du = (u1 - u0) * pad, dv = (v1 - v0) * pad;
      quad(S.bw * k * (1 + 2 * pad), S.bh * k * (1 + 2 * pad), [u0 - du, v0 - dv, u1 + du, v1 + dv], col, a, 0.35, 0, S.lift);
    };

    /* this frame's quads: everything is a function of the time since the bus was picked */
    function build(now) {
      n = 0;
      const S = shown; if (!S) return 0;
      const fade = cur ? sstep(0, 0.35, now - t0) : 1 - sstep(0, 0.3, now - tOff);
      if (fade <= 0) return 0;
      const { mk, discR, bw, z } = S, c = S.col;
      const light = [c[0] + (1 - c[0]) * 0.2, c[1] + (1 - c[1]) * 0.2, c[2] + (1 - c[2]) * 0.2];
      const tint = [c[0] + (1 - c[0]) * 0.12, c[1] + (1 - c[1]) * 0.12, c[2] + (1 - c[2]) * 0.12];
      const zr = clamp((16.5 - z) / 1.5), ringW = zr * fade, glowW = (1 - mk) * fade, dy = S.lift * 0.7 * (1 - mk);
      const el = now - t0;
      // zoomed out: a halo, and rings rippling out of the bus, two at a time
      if (mk > 0) quad(discR * 5.2, discR * 5.2, SQ, tint, 0.32 * mk * fade, 0.3, 1, 0);
      if (ringW > 0) {
        const R0 = mk * (discR + 1) + (1 - mk) * Math.max(bw * 0.42, 9);
        const R1 = Math.max(mk * discR * 4.6 + (1 - mk) * bw * 1.35, 40);
        const ps = CALM ? [0.35] : [(el / 2.6) % 1];
        if (!CALM && el >= 1.3) ps.push((el / 2.6 + 0.5) % 1);       // the second ring starts half a beat later
        for (const p of ps) {
          ring(R0 + (R1 - R0) * outCubic(p), 1.8, tint, ringW * 0.9 * Math.pow(1 - p, 1.6) * sstep(0, 0.05, p), dy);
        }
      }
      // zoomed in: the bus glows every few seconds, and light leaves its outline, swells, rounds into a ring and fades
      if (glowW > 0) {
        const P = 3.2, ph = CALM ? 0.5 : (el / P) % 1;
        const g = CALM ? 0.4 : Math.exp(-Math.pow((ph - 0.1) / 0.1, 2)) + Math.exp(-Math.pow((ph - 1.1) / 0.1, 2));
        outline(S, 1.04, light, glowW * (0.22 + 0.78 * g));
        if (!CALM) {
          const pe = clamp((ph - 0.05) / 0.8);
          if (pe > 0 && pe < 1) {
            outline(S, 1 + 0.6 * outCubic(pe), light, glowW * 0.85 * Math.pow(1 - pe, 2));
            ring(bw * 0.36 + (bw * 1.2 - bw * 0.36) * outCubic(pe), 2, light,
              glowW * (1 - zr) * 0.8 * sstep(0.06, 0.3, pe) * Math.pow(1 - pe, 1.4), S.lift * 0.7);
          }
        }
      }
      return n;
    }

    const layer = {
      id: opts.id || 'sel-fx', type: 'custom', renderingMode: '2d',
      onAdd(m, gl) {
        const sh = (t, src) => { const s = gl.createShader(t); gl.shaderSource(s, src); gl.compileShader(s); return s; };
        const pr = gl.createProgram();
        gl.attachShader(pr, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(pr);
        this.pr = pr; this.buf = gl.createBuffer(); this.loc = {};
        for (const a of ['a_pos', 'a_off', 'a_uv', 'a_col', 'a_mode']) this.loc[a] = gl.getAttribLocation(pr, a);
        for (const u of ['u_matrix', 'u_view', 'u_tex', 'u_rect', 'u_tap']) this.loc[u] = gl.getUniformLocation(pr, u);
      },
      render(gl, matrix) {
        if (!on || !shown) return;
        const now = performance.now() / 1000;
        if (!build(now)) { if (!cur) shown = null; return; }
        const tex = opts.tex && opts.tex(); if (!tex) return;
        const l = this.loc;
        gl.useProgram(this.pr);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
        gl.bufferData(gl.ARRAY_BUFFER, buf.subarray(0, n * STRIDE), gl.DYNAMIC_DRAW);
        const F = 4, attr = (k, size, off) => { gl.enableVertexAttribArray(l[k]); gl.vertexAttribPointer(l[k], size, gl.FLOAT, false, STRIDE * F, off * F); };
        attr('a_pos', 2, 0); attr('a_off', 2, 2); attr('a_uv', 2, 4); attr('a_col', 4, 6); attr('a_mode', 1, 10);
        // the one point we draw is the origin: fold its position into the matrix (full float precision)
        const M = Array.from(matrix), rx = mercX(shown.lng), ry = mercY(shown.lat);
        M[12] = matrix[0] * rx + matrix[4] * ry + matrix[12]; M[13] = matrix[1] * rx + matrix[5] * ry + matrix[13];
        M[14] = matrix[2] * rx + matrix[6] * ry + matrix[14]; M[15] = matrix[3] * rx + matrix[7] * ry + matrix[15];
        gl.uniformMatrix4fv(l.u_matrix, false, new Float32Array(M));
        const cv = map.getCanvas();
        gl.uniform2f(l.u_view, cv.clientWidth, cv.clientHeight);
        gl.uniform4f(l.u_rect, Math.min(rect[0], rect[2]), Math.min(rect[1], rect[3]), Math.max(rect[0], rect[2]), Math.max(rect[1], rect[3]));
        gl.uniform2f(l.u_tap, Math.abs(rect[2] - rect[0]) * 0.022, Math.abs(rect[3] - rect[1]) * 0.022);
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tex); gl.uniform1i(l.u_tex, 0);
        gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
        gl.drawArrays(gl.TRIANGLES, 0, n);
        for (const k of ['a_pos', 'a_off', 'a_uv', 'a_col', 'a_mode']) gl.disableVertexAttribArray(l[k]);
        tick();
      },
    };
    // while something is drawn, the next frame is asked for at once (a phone: ~30 a second, to stay cool)
    function tick() {
      if (CALM && cur && performance.now() / 1000 - t0 > 0.4) return;
      if (!MOBILE) { map.triggerRepaint(); return; }
      if (pending) return; pending = true;
      setTimeout(() => { pending = false; map.triggerRepaint(); }, 33);
    }
    function set(s) {
      const now = performance.now() / 1000;
      if (s) {
        // a new pick starts from the first ripple; the same bus picked again while it fades out carries on
        if (cur ? cur.id !== s.id : !shown || shown.id !== s.id || now - tOff >= 0.3) t0 = now;
        cur = shown = s; rect = s.rect || SQ;
      } else if (cur) { cur = null; tOff = now; }
      else return;
      map.triggerRepaint();
    }
    function add(above) { if (!map.getLayer(layer.id)) map.addLayer(layer, above); }
    return { layer, add, set, show(v) { on = v; map.triggerRepaint(); } };
  }
  return { create };
})();
