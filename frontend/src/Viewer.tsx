import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

/* One viewer for every stage. Node names in the GLB are occurrence ids (PN_n), the same ids the IPL rows,
   sequence steps and procedure steps carry, so selection is shared by id.
   STEP coordinates are Z-up millimetres; the GLB keeps them in metres, and the root is turned Z-up -> Y-up here. */

export interface ViewerProps {
  url: string | null;
  selected: Set<string>;                      // occ ids drawn in chromate
  tint?: Map<string, string>;                 // occ id -> CSS colour (status colouring)
  dim?: Set<string> | null;                   // when set, everything else is ghosted
  explode?: Map<string, [number, number, number]>; // occ id -> STEP direction (unit), for exploded views
  explodeAmount?: number;                     // 0..1
  hidden?: Set<string>;                       // removed parts (sequence playback)
  onPick?: (occId: string | null, additive: boolean) => void;
  onNodes?: (ids: string[]) => void;          // occurrence ids present in the loaded model
}

const BASE = new THREE.Color("#a9b2bc");
const SEL = new THREE.Color("#e3b505");

export function Viewer({ url, selected, tint, dim, explode, explodeAmount = 0, hidden, onPick, onNodes }: ViewerProps) {
  const mount = useRef<HTMLDivElement>(null);
  const st = useRef<{ renderer?: THREE.WebGLRenderer; scene?: THREE.Scene; camera?: THREE.PerspectiveCamera;
    controls?: OrbitControls; root?: THREE.Group; nodes: Map<string, THREE.Object3D>; mats: Map<string, THREE.MeshStandardMaterial[]>;
    home: Map<string, THREE.Vector3>; size: number; raf?: number }>({ nodes: new Map(), mats: new Map(), home: new Map(), size: 1 });
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [err, setErr] = useState("");
  const [hover, setHover] = useState<string | null>(null);
  const reduceMotion = useMemo(() => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches, []);

  // ---- renderer (once)
  useEffect(() => {
    const el = mount.current!;
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor("#e3e7eb");
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(35, 1, 0.0005, 100);
    camera.position.set(0.25, 0.2, 0.3);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = !reduceMotion;
    scene.add(new THREE.HemisphereLight("#ffffff", "#8b96a3", 1.6));
    const key = new THREE.DirectionalLight("#ffffff", 1.6); key.position.set(1, 2, 1.5); scene.add(key);
    const rim = new THREE.DirectionalLight("#dfe7ff", 0.6); rim.position.set(-1.5, 0.5, -1); scene.add(rim);
    st.current = { ...st.current, renderer, scene, camera, controls };
    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h, false); renderer.domElement.style.width = "100%"; renderer.domElement.style.height = "100%";
      camera.aspect = w / Math.max(h, 1); camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(resize); ro.observe(el); resize();
    const loop = () => { controls.update(); renderer.render(scene, camera); st.current.raf = requestAnimationFrame(loop); };
    loop();
    return () => { ro.disconnect(); cancelAnimationFrame(st.current.raf!); renderer.dispose(); el.removeChild(renderer.domElement); };
  }, [reduceMotion]);

  // ---- model
  useEffect(() => {
    const s = st.current;
    if (!s.scene) return;
    if (s.root) { s.scene.remove(s.root); s.root = undefined; }
    s.nodes.clear(); s.mats.clear(); s.home.clear();
    if (!url) { setStatus("idle"); onNodes?.([]); return; }
    setStatus("loading");
    new GLTFLoader().load(url, (gltf) => {
      const root = new THREE.Group();
      root.rotation.x = -Math.PI / 2;               // STEP Z-up -> viewer Y-up
      root.add(gltf.scene);
      gltf.scene.traverse((o) => {
        if (o.name.startsWith("hidden:")) o.visible = false;      // manufacturing phases / stock of a machined part
        if (/^[A-Za-z0-9-]+_\d+$/.test(o.name)) s.nodes.set(o.name, o);
        const m = o as THREE.Mesh;
        if (m.isMesh) {
          const mat = new THREE.MeshStandardMaterial({ color: BASE.clone(), metalness: 0.35, roughness: 0.5, transparent: true });
          m.material = mat;
          let p: THREE.Object3D | null = m;
          while (p && !s.nodes.has(p.name) && !/^[A-Za-z0-9-]+_\d+$/.test(p.name)) p = p.parent;
          const id = p?.name ?? m.name;
          if (!s.mats.has(id)) s.mats.set(id, []);
          s.mats.get(id)!.push(mat);
          m.userData.occ = id;
        }
      });
      s.scene!.add(root); s.root = root;
      root.updateMatrixWorld(true);
      s.nodes.forEach((n, id) => s.home.set(id, n.position.clone()));
      const box = new THREE.Box3().setFromObject(root);
      const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3()).length();
      s.size = size;
      s.controls!.target.copy(c);
      s.camera!.position.copy(c.clone().add(new THREE.Vector3(0.62, 0.5, 0.95).normalize().multiplyScalar(size * 1.75)));
      s.camera!.near = size / 200; s.camera!.far = size * 50; s.camera!.updateProjectionMatrix();
      setStatus("ready");
      onNodes?.([...s.nodes.keys()]);
    }, undefined, (e) => { setErr(String((e as Error)?.message ?? e)); setStatus("error"); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  // ---- colours, selection, ghosting, hiding
  useEffect(() => {
    const s = st.current;
    s.mats.forEach((mats, id) => {
      const isSel = selected.has(id);
      const t = tint?.get(id);
      const ghost = dim && !dim.has(id) && !isSel;
      for (const m of mats) {
        m.color.copy(isSel ? SEL : t ? new THREE.Color(t) : BASE);
        m.emissive.copy(isSel ? new THREE.Color("#5a4300") : id === hover ? new THREE.Color("#2a3440") : new THREE.Color("#000000"));
        m.opacity = ghost ? 0.12 : 1;
        m.depthWrite = !ghost;
      }
    });
    s.nodes.forEach((n, id) => { n.visible = !(hidden?.has(id)); });
  }, [selected, tint, dim, hover, hidden, status]);

  // ---- exploded view: offset each part along its removal direction (STEP frame = root-local frame)
  useEffect(() => {
    const s = st.current;
    if (!s.root) return;
    s.root.updateMatrixWorld(true);
    s.nodes.forEach((n, id) => {
      const home = s.home.get(id);
      if (!home) return;
      n.position.copy(home);
      const d = explode?.get(id);
      if (d && explodeAmount > 0 && n.parent) {
        n.parent.updateMatrixWorld(true);
        const worldHome = n.parent.localToWorld(home.clone());
        const dir = new THREE.Vector3(...d).applyQuaternion(s.root!.quaternion).normalize();
        const target = worldHome.add(dir.multiplyScalar(s.size * 0.45 * explodeAmount));
        n.position.copy(n.parent.worldToLocal(target));
      }
    });
  }, [explode, explodeAmount, status]);

  // ---- picking
  const pick = (ev: React.MouseEvent, click: boolean) => {
    const s = st.current;
    if (!s.root || !s.camera) return;
    const r = (ev.currentTarget as HTMLElement).getBoundingClientRect();
    const ndc = new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    const ray = new THREE.Raycaster(); ray.setFromCamera(ndc, s.camera);
    const hit = ray.intersectObject(s.root, true).find((h) => {
      const m = (h.object as THREE.Mesh).material as THREE.MeshStandardMaterial;
      let o: THREE.Object3D | null = h.object;
      while (o) { if (!o.visible) return false; o = o.parent; }      // hidden parts and their parents' hidden bodies
      return !m || m.opacity > 0.5;
    });
    const id = (hit?.object.userData.occ as string) ?? null;
    if (click) onPick?.(id, ev.shiftKey || ev.metaKey || ev.ctrlKey);
    else setHover(id);
  };
  const down = useRef<[number, number]>([0, 0]);

  const fit = () => {
    const s = st.current;
    if (!s.root) return;
    const objs = [...selected].map((id) => s.nodes.get(id)).filter(Boolean) as THREE.Object3D[];
    const box = new THREE.Box3();
    (objs.length ? objs : [s.root]).forEach((o) => box.expandByObject(o));
    const c = box.getCenter(new THREE.Vector3()), size = Math.max(box.getSize(new THREE.Vector3()).length(), s.size * 0.08);
    const dir = s.camera!.position.clone().sub(s.controls!.target).normalize();
    s.controls!.target.copy(c);
    s.camera!.position.copy(c.clone().add(dir.multiplyScalar(size * 1.9)));
  };

  return (
    <div className="viewer">
      <div ref={mount} className="viewer-canvas"
        onMouseDown={(e) => { down.current = [e.clientX, e.clientY]; }}
        onMouseUp={(e) => { if (Math.abs(e.clientX - down.current[0]) + Math.abs(e.clientY - down.current[1]) < 4) pick(e, true); }}
        onMouseMove={(e) => pick(e, false)} />
      <div className="viewer-hud">
        {status === "loading" && <span>Loading the 3D model…</span>}
        {status === "error" && <span className="bad">The 3D model could not be loaded: {err}</span>}
        {status === "idle" && <span>The 3D model appears after the conversion.</span>}
        {status === "ready" && hover && <span className="hover-id">{hover}</span>}
      </div>
      {status === "ready" && <button className="viewer-fit" onClick={fit} title="Frame the selection (or the whole model)">Frame</button>}
    </div>
  );
}
