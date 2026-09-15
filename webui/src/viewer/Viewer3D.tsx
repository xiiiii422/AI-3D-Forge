import { useCallback, useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js';
import { toCreasedNormals } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { stlRawUrl } from '../api/client';

export interface ModelStats {
  /** 包围盒尺寸（mm） */
  size: { x: number; y: number; z: number };
  /** 三角面数 */
  triangles: number;
  /** 体积（mm³），由网格有向体积算出 */
  volumeMm3: number;
}

interface Viewer3DProps {
  /** 为空时展示空状态（不加载模型） */
  projectId: string | null;
  /** 版本号变化会重新拉取 STL（例如参数微调后） */
  revision?: number;
  /** 线框模式 */
  wireframe: boolean;
  /** 自动旋转 */
  autoRotate: boolean;
  /** 重置视角信号：数值变化即重置 */
  resetSignal: number;
  onStats: (stats: ModelStats | null) => void;
  onLoadingChange?: (loading: boolean) => void;
  onError?: (message: string | null) => void;
}

/** STL 默认单位按毫米理解（契约里 bbox 就是 mm） */
const MM = 1;

/**
 * STL 预览：三点影棚布光 + 中性台面 + 网格 + 接地阴影，OrbitControls 旋转/平移/缩放。
 * 刻意不使用 react-three-fiber —— 用命令式 three + React 包装，生命周期更容易掌控。
 */
export default function Viewer3D({
  projectId,
  revision,
  wireframe,
  autoRotate,
  resetSignal,
  onStats,
  onLoadingChange,
  onError,
}: Viewer3DProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const coreRef = useRef<{
    renderer: THREE.WebGLRenderer;
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    controls: OrbitControls;
    grid: THREE.GridHelper;
    ground: THREE.Mesh;
    model: THREE.Group;
  } | null>(null);
  const loaderRef = useRef<STLLoader | null>(null);
  const resetViewRef = useRef<() => void>(() => undefined);
  /** 已经处理过的 resetSignal，用于区分「首次渲染」与「用户点击重置」 */
  const lastResetRef = useRef(0);
  const [ready, setReady] = useState(false);

  // ---- 场景初始化（只做一次） ----
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: false,
      powerPreference: 'high-performance',
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.0;
    host.appendChild(renderer.domElement);
    renderer.domElement.setAttribute('aria-label', '3D 模型预览');

    const scene = new THREE.Scene();
    const styles = getComputedStyle(host);
    const stageBg = new THREE.Color(styles.getPropertyValue('--viewer-bg').trim() || '#f2f3f5');
    scene.background = stageBg;

    const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 20000);
    camera.position.set(150, 130, 210);

    // ---- 三点影棚布光 ----
    const key = new THREE.DirectionalLight(0xffffff, 2.5);
    key.position.set(180, 260, 200);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.camera.near = 10;
    key.shadow.camera.far = 1600;
    key.shadow.camera.left = -320;
    key.shadow.camera.right = 320;
    key.shadow.camera.top = 320;
    key.shadow.camera.bottom = -320;
    key.shadow.bias = -0.0006;
    key.shadow.normalBias = 0.02;

    const fill = new THREE.DirectionalLight(0xffffff, 0.85);
    fill.position.set(-240, 120, 140);

    const rim = new THREE.DirectionalLight(0xffffff, 1.2);
    rim.position.set(-90, 170, -260);

    const hemi = new THREE.HemisphereLight(0xffffff, 0x9a9aa2, 0.55);

    scene.add(key, fill, rim, hemi);

    // ---- 台面与网格 ----
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(6000, 6000),
      new THREE.ShadowMaterial({ opacity: 0.16 }),
    );
    ground.rotation.x = -Math.PI / 2;
    ground.receiveShadow = true;
    scene.add(ground);

    const grid = new THREE.GridHelper(400, 40, 0x9a9aa2, 0xc9c9cf);
    const gridMat = grid.material as THREE.Material;
    gridMat.transparent = true;
    gridMat.opacity = 0.5;
    scene.add(grid);

    const model = new THREE.Group();
    scene.add(model);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.07;
    controls.rotateSpeed = 0.85;
    controls.panSpeed = 0.8;
    controls.zoomSpeed = 0.9;
    controls.autoRotateSpeed = 1.6;
    controls.minDistance = 8;
    controls.maxDistance = 6000;
    controls.target.set(0, 40, 0);

    coreRef.current = { renderer, scene, camera, controls, grid, ground, model };
    loaderRef.current = new STLLoader();
    setReady(true);

    // ---- 尺寸自适应 ----
    const resize = () => {
      const w = Math.max(host.clientWidth, 1);
      const h = Math.max(host.clientHeight, 1);
      renderer.setSize(w, h, false);
      renderer.domElement.style.width = '100%';
      renderer.domElement.style.height = '100%';
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();

    const observer = new ResizeObserver(resize);
    observer.observe(host);
    document.addEventListener('fullscreenchange', resize);
    // OrbitControls 用右键平移，屏蔽系统右键菜单
    const onContextMenu = (e: Event) => e.preventDefault();
    host.addEventListener('contextmenu', onContextMenu);

    let raf = 0;
    const tick = () => {
      raf = requestAnimationFrame(tick);
      controls.update();
      renderer.render(scene, camera);
    };
    tick();

    /** 把相机移到能完整框住 currentSize 的位置 */
    resetViewRef.current = () => {
      const box = new THREE.Box3().setFromObject(model);
      controls.target.set(0, 40, 0);
      if (box.isEmpty()) {
        camera.position.set(150, 130, 210);
      } else {
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        const radius = Math.max(size.length() * 0.5, 20);
        const dist = radius / Math.sin((camera.fov * Math.PI) / 360) + radius * 0.25;
        const dir = new THREE.Vector3(0.62, 0.5, 0.86).normalize();
        camera.position.copy(center).addScaledVector(dir, dist);
        controls.target.copy(center);
      }
      camera.near = Math.max(controls.target.distanceTo(camera.position) / 800, 0.2);
      camera.far = controls.target.distanceTo(camera.position) * 40;
      camera.updateProjectionMatrix();
      controls.update();
    };

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
      document.removeEventListener('fullscreenchange', resize);
      host.removeEventListener('contextmenu', onContextMenu);
      controls.dispose();
      disposeGroup(model);
      ground.geometry.dispose();
      (ground.material as THREE.Material).dispose();
      grid.geometry.dispose();
      gridMat.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode === host) host.removeChild(renderer.domElement);
      coreRef.current = null;
    };
  }, []);

  // ---- 加载模型 ----
  useEffect(() => {
    const core = coreRef.current;
    if (!core || !ready) return;

    disposeGroup(core.model);
    onStats(null);
    onError?.(null);

    if (!projectId) {
      resetViewRef.current();
      onLoadingChange?.(false);
      return;
    }

    let cancelled = false;
    onLoadingChange?.(true);

    fetch(stlRawUrl(projectId, revision), { headers: { Accept: 'application/sla, model/stl, */*' } })
      .then(async (res) => {
        if (!res.ok) throw new Error(`预览加载失败（${res.status}）`);
        return res.arrayBuffer();
      })
      .then((buffer) => {
        if (cancelled) return;
        if (buffer.byteLength === 0) throw new Error('模型文件为空。');
        const raw = new STLLoader().parse(buffer);
        // 法线必须按「折痕角度」算，不能直接 computeVertexNormals()。
        //
        // STL 里每个三角面自带三个**互不共享**的顶点，直接算顶点法线得到的是
        // 面法线 = 平面着色：一个 79 段的圆柱在屏幕上就是 79 个平面，
        // 用户会以为"圆根本没画圆"（用户反馈原话：「建模圆形时边数太少，
        // 完全达不到圆的效果」）。toCreasedNormals 会在夹角小于阈值的相邻面
        // 之间做平滑（曲面变圆滑），同时保留真正的棱角（立方体的边不会被抹圆）。
        const geometry = toCreasedNormals(raw, THREE.MathUtils.degToRad(30));
        raw.dispose();
        geometry.computeBoundingBox();

        const material = new THREE.MeshStandardMaterial({
          color: 0xb9bcc2,
          roughness: 0.48,
          metalness: 0.12,
          flatShading: false,
          side: THREE.DoubleSide,
        });

        const mesh = new THREE.Mesh(geometry, material);
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        mesh.name = 'stl';

        // 描边：让模型轮廓在浅色台面上更清晰（线框模式直接用它）
        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geometry, 28),
          new THREE.LineBasicMaterial({ color: 0x6e6e73, transparent: true, opacity: 0.5 }),
        );
        edges.name = 'edges';
        edges.visible = false;

        const group = new THREE.Group();
        group.add(mesh, edges);
        core.model.add(group);

        const box = geometry.boundingBox ?? new THREE.Box3();
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        // 平移到原点、底面贴地：视角与网格关系稳定
        group.position.set(-center.x, -box.min.y, -center.z);

        // 台面与网格随模型尺度调整，避免出现「小模型大网格」的观感
        const footprint = Math.max(size.x, size.z, 1);
        const gridStep = niceStep(footprint / 6);
        core.scene.remove(core.grid);
        core.grid.geometry.dispose();
        (core.grid.material as THREE.Material).dispose();
        const nextGrid = new THREE.GridHelper(
          gridStep * 20,
          20,
          0x9a9aa2,
          0xc9c9cf,
        );
        const nextMat = nextGrid.material as THREE.Material;
        nextMat.transparent = true;
        nextMat.opacity = 0.5;
        core.grid = nextGrid;
        core.scene.add(nextGrid);

        // 阴影接收面放在模型底面高度
        core.ground.position.y = 0;

        const position = geometry.getAttribute('position');
        onStats({
          size: { x: size.x, y: size.y, z: size.z },
          triangles: Math.floor(position.count / 3),
          volumeMm3: Math.abs(computeVolume(geometry)),
        });
        onLoadingChange?.(false);
        resetViewRef.current();
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        onLoadingChange?.(false);
        onError?.(err instanceof Error ? err.message : '模型预览加载失败。');
      });

    return () => {
      cancelled = true;
    };
  }, [projectId, revision, ready, onStats, onLoadingChange, onError]);

  // ---- 线框 / 自动旋转 / 重置视角 ----
  useEffect(() => {
    const core = coreRef.current;
    if (!core) return;
    core.model.traverse((obj) => {
      if (obj.name === 'stl' && obj instanceof THREE.Mesh) obj.visible = !wireframe;
      if (obj.name === 'edges' && obj instanceof THREE.LineSegments) obj.visible = true;
    });
  }, [wireframe, ready, projectId, revision]);

  useEffect(() => {
    const core = coreRef.current;
    if (!core) return;
    core.controls.autoRotate = autoRotate;
  }, [autoRotate, ready]);

  const resetView = useCallback(() => resetViewRef.current(), []);

  useEffect(() => {
    if (!ready) return;
    if (resetSignal !== lastResetRef.current) {
      lastResetRef.current = resetSignal;
      resetView();
    }
  }, [resetSignal, ready, resetView]);

  return <div className="viewer-canvas" ref={hostRef} />;
}

/** 释放一个分组下所有几何体/材质，避免反复预览时泄漏显存 */
function disposeGroup(group: THREE.Object3D): void {
  const doomed: THREE.Object3D[] = [...group.children];
  for (const child of doomed) {
    group.remove(child);
    child.traverse((obj) => {
      if (obj instanceof THREE.Mesh || obj instanceof THREE.LineSegments) {
        obj.geometry.dispose();
        const mat = obj.material;
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
        else mat.dispose();
      }
    });
  }
}

/** 网格步长取整到 1/2/5 × 10^n，保证刻度是「好看的数字」 */
function niceStep(raw: number): number {
  const value = Math.max(raw, 0.01);
  const exp = Math.floor(Math.log10(value));
  const base = Math.pow(10, exp);
  const norm = value / base;
  if (norm <= 1) return base;
  if (norm <= 2) return 2 * base;
  if (norm <= 5) return 5 * base;
  return 10 * base;
}

/**
 * 闭合网格体积：对每个三角形取原点四面体的有向体积求和（mm³）。
 * STL 是三角网格，这个算法与后端体积口径一致，无需额外依赖。
 */
function computeVolume(geometry: THREE.BufferGeometry): number {
  const pos = geometry.getAttribute('position');
  if (!pos) return 0;
  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  const cross = new THREE.Vector3();
  let total = 0;
  for (let i = 0; i < pos.count; i += 3) {
    a.fromBufferAttribute(pos, i);
    b.fromBufferAttribute(pos, i + 1);
    c.fromBufferAttribute(pos, i + 2);
    cross.copy(b).cross(c);
    total += a.dot(cross) / 6;
  }
  return total * MM * MM * MM;
}
