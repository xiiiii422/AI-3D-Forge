import { useCallback, useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js';
import { toCreasedNormals } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { stlRawUrl } from '../api/client';
import {
  annotationSegments,
  buildOverallDimensions,
  formatMm,
  type DimensionAnnotation,
} from './dimensions';

export interface ModelStats {
  /**
   * 三向尺寸（mm）。语义与后端一致：宽=X、深=Y、高=Z（STL 原始轴），
   * 之前按 three 的 x/y/z 直接打印，会把「高」写成深度。
   */
  size: { width: number; depth: number; height: number };
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
  /** 尺寸标注：未选中棱线时标出整体宽/深/高，点中某条棱线则只标该边长度 */
  showDimensions?: boolean;
  onStats: (stats: ModelStats | null) => void;
  onLoadingChange?: (loading: boolean) => void;
  onError?: (message: string | null) => void;
}

/** STL 默认单位按毫米理解（契约里 bbox 就是 mm） */
const MM = 1;

/**
 * STL 预览：三点影棚布光 + 中性台面 + 网格 + 接地阴影，OrbitControls 旋转/平移/缩放。
 * 刻意不使用 react-three-fiber —— 用命令式 three + React 包装，生命周期更容易掌控。
 *
 * 实时尺寸标注：模型外围画出工程图式的尺寸线（宽/深/高），
 * 点中任意一条棱线则高亮它并只报这条边的长度。标签是 HTML 叠层，
 * 每帧把世界锚点投影到屏幕 —— 文字因此始终是锐利的（跟着 3D 走，但不进纹理）。
 */
export default function Viewer3D({
  projectId,
  revision,
  wireframe,
  autoRotate,
  resetSignal,
  showDimensions = true,
  onStats,
  onLoadingChange,
  onError,
}: Viewer3DProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const layerRef = useRef<HTMLDivElement>(null);
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

  /** 标注数据（当前模型的三条整体尺寸），开关切换时据此重建线段 */
  const annotationRef = useRef<DimensionAnnotation[]>([]);
  /** 拾取要用的当前模型对象与世界尺度 */
  const pickingRef = useRef<{
    mesh: THREE.Mesh | null;
    edges: THREE.LineSegments | null;
    footprint: number;
  }>({ mesh: null, edges: null, footprint: 100 });
  const labelsRef = useRef<{ el: HTMLSpanElement; anchor: THREE.Vector3; lift: number }[]>([]);
  /** 选中边的长度（mm）与标签锚点；null = 没有选中，回到整体尺寸 */
  const selectedRef = useRef<{ lengthMm: number; anchor: THREE.Vector3 } | null>(null);
  const showDimsRef = useRef(showDimensions);
  /**
   * 线框开关的当前值，供**模型加载回调**读取。
   *
   * 为什么需要它（实测缺陷）：线框那个 effect 依赖 `[wireframe, ready, projectId, revision]`，
   * 它在 projectId/revision 变化时就会跑 —— 但那时新模型还在 `fetch(...).then()` 里，
   * `core.model` 是空的，traverse 不到任何东西，等于白跑一遍。等 STL 真正解析完、
   * group 被加进场景时，没人再按当前的线框状态设置一次可见性，于是新建的模型
   * 一律用默认值渲染（下面 `edges.visible = false` + 网格可见）。
   * 表现：**开着线框切换/生成模型，新模型却显示成实心**，开关像是"失灵"了。
   *
   * 解法与 `showDimsRef` 同源：把状态同步进 ref，异步回调建对象时直接读 ref，
   * 不依赖 effect 的执行时机。
   */
  const wireframeRef = useRef(wireframe);
  /** 选中态同时留一份 React 状态：给屏幕阅读器与「取消选择」按钮用 */
  const [selectedMm, setSelectedMm] = useState<number | null>(null);
  /** 当前画面里有没有可标注的模型：空状态下不该出现「点击棱线」的引导 */
  const [hasAnnotations, setHasAnnotations] = useState(false);
  /** 由初始化 effect 填入，供外部（取消按钮）复用同一套清理逻辑 */
  const clearSelectionRef = useRef<() => void>(() => undefined);
  /** 标签内容变化时重建 DOM（模型加载完、选中变化、开关切换） */
  const rebuildLabelsRef = useRef<() => void>(() => undefined);
  /** 尺寸线组的重建（颜色/材质住在初始化 effect 里，所以由它提供） */
  const rebuildDimsRef = useRef<() => void>(() => undefined);
  /** 逼预览重新加载：验收脚本用它走一遍 dispose → fetch → 建对象 的完整路径 */
  const [reloadNonce, setReloadNonce] = useState(0);

  /**
   * 验收钩子：把内部场景与"重新加载"入口挂到 window 上。
   *
   * 为什么需要：线框这类开关的 bug **不在 DOM 上**。按钮的 aria-pressed 会正确
   * 变成 true，而场景里的模型还是实心的 —— 只查按钮的验收步骤永远发现不了。
   * 要验它就必须能读出场景里网格/描边的真实 `visible`，所以留这个只读钩子。
   *
   * 它不做任何产品逻辑，仅暴露已有状态；卸载时清掉，避免残留。
   */
  useEffect(() => {
    (window as unknown as Record<string, unknown>).__aiforgeViewer = {
      coreRef,
      bumpRevision: () => setReloadNonce((n) => n + 1),
    };
    return () => {
      delete (window as unknown as Record<string, unknown>).__aiforgeViewer;
    };
  }, []);

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

    // ---- 实时尺寸标注 ----
    // 颜色从设计令牌取（tokens.css 是唯一允许声明颜色的地方）；
    // 主题在设置页切换后回到本页会重新挂载组件，所以这里读一次就够。
    const dimStyle = getComputedStyle(host);
    const dimColor = new THREE.Color(dimStyle.getPropertyValue('--text-3').trim() || '#8a8a93');
    const accentColor = new THREE.Color(
      dimStyle.getPropertyValue('--brand').trim() || '#2f6feb',
    );

    const raycaster = new THREE.Raycaster();
    const pointerNdc = new THREE.Vector2();
    const camDir = new THREE.Vector3();
    const toPoint = new THREE.Vector3();
    const ndcPoint = new THREE.Vector3();

    /** 标签是 HTML 叠层：文字变化时才重建，位置每帧投影更新 */
    const rebuildLabels = () => {
      const layer = layerRef.current;
      if (!layer) return;
      layer.textContent = '';
      const items: { dim: string; text: string; anchor: THREE.Vector3; edge: boolean }[] = [];
      const selected = selectedRef.current;
      if (selected) {
        // 选中一条棱线时只报这条边，整体尺寸退到浮层里，避免一屏数字
        items.push({
          dim: 'edge',
          text: `该边 ${formatMm(selected.lengthMm)} mm`,
          anchor: selected.anchor,
          edge: true,
        });
      } else if (showDimsRef.current) {
        for (const item of annotationRef.current) {
          items.push({
            dim: item.axis,
            text: item.text,
            anchor: new THREE.Vector3(item.anchor[0], item.anchor[1], item.anchor[2]),
            edge: false,
          });
        }
      }
      labelsRef.current = items.map((entry) => {
        const el = document.createElement('span');
        el.className = entry.edge ? 'dim-tag is-edge' : 'dim-tag';
        el.dataset.dim = entry.dim;
        el.textContent = entry.text;
        layer.appendChild(el);
        // 该边的标签往上抬一点：它本来就在高亮柱正中间，压上去两边都看不清。
        return { el, anchor: entry.anchor, lift: entry.edge ? 15 : 0 };
      });
    };
    rebuildLabelsRef.current = rebuildLabels;

    /** 按 annotationRef 里的数据重建尺寸线组（挂在 model 下，跟着模型一起变换） */
    const rebuildDims = () => {
      const old = model.getObjectByName('dims');
      if (old) {
        model.remove(old);
        disposeGroup(old);
      }
      if (!showDimsRef.current || !annotationRef.current.length) return;
      const { dimLines, leaders } = annotationSegments(annotationRef.current);
      const group = new THREE.Group();
      group.name = 'dims';
      const lineMat = new THREE.LineBasicMaterial({
        color: dimColor,
        transparent: true,
        opacity: 0.95,
        toneMapped: false,
      });
      // 引出线比尺寸线淡：工程图里它只是辅线，不该跟尺寸线抢注意力
      const leaderMat = new THREE.LineBasicMaterial({
        color: dimColor,
        transparent: true,
        opacity: 0.45,
        toneMapped: false,
      });
      group.add(segmentsFrom(dimLines, lineMat), segmentsFrom(leaders, leaderMat));
      model.add(group);
    };
    rebuildDimsRef.current = rebuildDims;

    const clearEdgeHighlight = () => {
      const old = model.getObjectByName('edge-hl');
      if (!old) return;
      model.remove(old);
      const mesh = old as THREE.Mesh;
      mesh.geometry.dispose();
      (mesh.material as THREE.Material).dispose();
    };

    /** 把选中的棱线画成一段实心短柱：WebGL 里 line 宽度恒为 1px，高亮必须用几何体 */
    const applyEdgeHighlight = (a: THREE.Vector3, b: THREE.Vector3) => {
      clearEdgeHighlight();
      const dir = b.clone().sub(a);
      const len = dir.length();
      if (len <= 0) return;
      const radius = Math.max(pickingRef.current.footprint * 0.006, 0.3);
      const cylinder = new THREE.Mesh(
        new THREE.CylinderGeometry(radius, radius, len, 10, 1, true),
        new THREE.MeshBasicMaterial({ color: accentColor, toneMapped: false }),
      );
      cylinder.name = 'edge-hl';
      cylinder.position.copy(a).addScaledVector(dir, 0.5);
      cylinder.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.normalize());
      model.add(cylinder);
    };

    const clearSelection = () => {
      if (!selectedRef.current) return;
      selectedRef.current = null;
      clearEdgeHighlight();
      rebuildLabels();
      setSelectedMm(null);
    };
    clearSelectionRef.current = clearSelection;

    /**
     * 屏幕坐标 → 命中的棱线。
     * withOcclusion：被模型挡在后面的棱线不算选中（悬停时为了跟手会跳过这步）。
     */
    const pickEdge = (clientX: number, clientY: number, withOcclusion = true) => {
      const { mesh, edges, footprint } = pickingRef.current;
      if (!edges || !showDimsRef.current) return null;
      const rect = renderer.domElement.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      pointerNdc.set(
        ((clientX - rect.left) / rect.width) * 2 - 1,
        -((clientY - rect.top) / rect.height) * 2 + 1,
      );
      raycaster.setFromCamera(pointerNdc, camera);
      // 容差按屏幕像素换算成世界单位：无论放大缩小，「点得中」的难度保持恒定
      const toTarget = raycaster.ray.origin.distanceTo(controls.target);
      const worldPerPixel =
        (2 * toTarget * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) / rect.height;
      raycaster.params.Line.threshold = Math.max(worldPerPixel * 8, footprint * 0.001);
      const hit = raycaster.intersectObject(edges, false).find((h) => h.index !== undefined);
      if (!hit || hit.index === undefined) return null;
      if (withOcclusion && mesh) {
        const surface = raycaster.intersectObject(mesh, false);
        if (surface.length && hit.distance > surface[0].distance + worldPerPixel * 10) return null;
      }
      const position = edges.geometry.getAttribute('position');
      const a = new THREE.Vector3().fromBufferAttribute(position, hit.index);
      const b = new THREE.Vector3().fromBufferAttribute(position, hit.index + 1);
      edges.updateWorldMatrix(true, false);
      a.applyMatrix4(edges.matrixWorld);
      b.applyMatrix4(edges.matrixWorld);
      const lengthMm = a.distanceTo(b);
      if (!(lengthMm > 0)) return null;
      return { a, b, lengthMm };
    };

    let downX = 0;
    let downY = 0;
    let downAt = 0;
    const onPointerDown = (event: PointerEvent) => {
      downX = event.clientX;
      downY = event.clientY;
      downAt = event.timeStamp;
    };
    const onPointerUp = (event: PointerEvent) => {
      if (event.button !== 0) return;
      // 拖拽旋转松手时不能当成「点选」
      if (Math.hypot(event.clientX - downX, event.clientY - downY) > 6) return;
      if (event.timeStamp - downAt > 900) return;
      const picked = pickEdge(event.clientX, event.clientY);
      if (!picked) {
        clearSelection();
        return;
      }
      selectedRef.current = {
        lengthMm: picked.lengthMm,
        anchor: picked.a.clone().add(picked.b).multiplyScalar(0.5),
      };
      applyEdgeHighlight(picked.a, picked.b);
      rebuildLabels();
      setSelectedMm(picked.lengthMm);
    };
    let hoverAt = 0;
    const onPointerMove = (event: PointerEvent) => {
      if (!showDimsRef.current || !pickingRef.current.edges) return;
      if ((event.buttons ?? 0) !== 0) return; // 拖拽中不做悬停拾取
      const now = event.timeStamp;
      if (now - hoverAt < 60) return; // 悬停只改光标，限频到 ~16 次/秒就够跟手
      hoverAt = now;
      const picked = pickEdge(event.clientX, event.clientY, false);
      renderer.domElement.style.cursor = picked ? 'pointer' : 'default';
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !selectedRef.current) return;
      // 弹层开着时 Esc 归弹层管，不能顺手把选中的边也清掉
      if (document.querySelector('.overlay')) return;
      clearSelection();
    };

    renderer.domElement.addEventListener('pointerdown', onPointerDown);
    renderer.domElement.addEventListener('pointerup', onPointerUp);
    renderer.domElement.addEventListener('pointermove', onPointerMove);
    window.addEventListener('keydown', onKeyDown);

    /**
     * 画面上的实底浮层（左下「模型数据」、左上「棱线点选提示」）会把压上去的
     * 尺寸标签整个吞掉（实测：宽 80.0 的标签完全看不见）。标签让开浮层，
     * 而不是把浮层压得更深 —— 浮层里的体积/耗材是另一回事，不能为了标注去掉。
     * 浮层宽高随内容（有没有耗材行）和窗口变化，300ms 重新量一次就够。
     */
    let keepOuts: { left: number; top: number; right: number; bottom: number }[] = [];
    let keepOutAt = 0;
    const refreshKeepOut = (now: number) => {
      if (now - keepOutAt < 300) return;
      keepOutAt = now;
      const stage = host.closest('.viewer-stage');
      const base = host.getBoundingClientRect();
      keepOuts = [
        ...(stage?.querySelectorAll('.viewer-overlay-stats, .viewer-dim-hint') ?? []),
      ].map((panel) => {
        const box = panel.getBoundingClientRect();
        return {
          left: box.left - base.left,
          top: box.top - base.top,
          right: box.right - base.left,
          bottom: box.bottom - base.top,
        };
      });
    };

    /** 世界锚点 → 屏幕像素；转到模型背后或画面外的标签直接隐掉 */
    const projectLabels = () => {
      const items = labelsRef.current;
      if (!items.length) return;
      const w = host.clientWidth || 1;
      const h = host.clientHeight || 1;
      refreshKeepOut(performance.now());
      camera.getWorldDirection(camDir);
      for (const item of items) {
        toPoint.copy(item.anchor).sub(camera.position);
        ndcPoint.copy(item.anchor).project(camera);
        const x = (ndcPoint.x * 0.5 + 0.5) * w;
        let y = (-ndcPoint.y * 0.5 + 0.5) * h - item.lift;
        const onScreen = toPoint.dot(camDir) > 0 && x > -48 && x < w + 48 && y > -28 && y < h + 28;
        item.el.style.display = onScreen ? '' : 'none';
        if (onScreen) {
          const lw = item.el.offsetWidth;
          const lh = item.el.offsetHeight;
          for (const block of keepOuts) {
            const hit =
              x + lw / 2 > block.left &&
              x - lw / 2 < block.right &&
              y + lh / 2 > block.top &&
              y - lh / 2 < block.bottom;
            if (!hit) continue;
            /* 挑靠哪一侧躲：看浮层贴在画面哪一半。
             * 只看「哪边距离短」会把贴着画面底部的标签推到浮层下方 ——
             * 那里只剩几个像素，实测「宽 80.0」就被挤到底边被裁掉一半。
             * 底下的浮层只能往上躲，顶上的只能往下躲，这跟它们的位置意图一致。 */
            const above = block.top - 6 - lh / 2;
            const below = block.bottom + 6 + lh / 2;
            y = (block.top + block.bottom) / 2 > h / 2 ? above : below;
            y = Math.min(Math.max(y, lh / 2 + 2), h - lh / 2 - 2);
          }
          item.el.style.transform = `translate(-50%, -50%) translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
        }
      }
    };

    // ---- 尺寸自适应 ----
    const resize = () => {
      const w = Math.max(host.clientWidth, 1);
      const h = Math.max(host.clientHeight, 1);
      // 重设像素比：窗口被拖到另一块屏、或用户在系统里改显示缩放时，
      // 不跟着改就会拿旧的后备缓冲拉伸，3D 线条在高 DPI 屏上发虚
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
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

    // 只改系统缩放不改窗口大小时往往不会触发 resize，用 dppx 媒体查询盯着它
    let dprQuery: MediaQueryList | null = null;
    const onDprChange = () => {
      resize();
      watchDpr();
    };
    function watchDpr() {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      if (dprQuery) dprQuery.removeEventListener('change', onDprChange);
      dprQuery = window.matchMedia(`(resolution: ${ratio}dppx)`);
      dprQuery.addEventListener('change', onDprChange);
    }
    watchDpr();
    // OrbitControls 用右键平移，屏蔽系统右键菜单
    const onContextMenu = (e: Event) => e.preventDefault();
    host.addEventListener('contextmenu', onContextMenu);

    let raf = 0;
    const tick = () => {
      raf = requestAnimationFrame(tick);
      controls.update();
      projectLabels();
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
      if (dprQuery) dprQuery.removeEventListener('change', onDprChange);
      host.removeEventListener('contextmenu', onContextMenu);
      renderer.domElement.removeEventListener('pointerdown', onPointerDown);
      renderer.domElement.removeEventListener('pointerup', onPointerUp);
      renderer.domElement.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('keydown', onKeyDown);
      controls.dispose();
      disposeGroup(model);
      ground.geometry.dispose();
      (ground.material as THREE.Material).dispose();
      // 网格在每次加载模型时都被换成新的（尺度要跟着模型走），所以这里要释放的是
      // **当前** core.grid 的几何与材质，不能用初始化时捕获的 grid/gridMat ——
      // 那些早已在换网格时被 dispose 掉了，真实的最后一个材质会一直泄漏。
      // （每次换模型都漏一个 LineBasicMaterial；一次会话只漏一个，但这是白漏。）
      grid.geometry.dispose();
      (grid.material as THREE.Material).dispose();
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
    annotationRef.current = [];
    selectedRef.current = null;
    pickingRef.current = { mesh: null, edges: null, footprint: 100 };
    setHasAnnotations(false);
    rebuildLabelsRef.current();

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
        // STL 必须转成 Y 向上才能放进这个场景。
        //
        // 实测依据：本机 8 个真实项目的 model.stl 都是 min.z ≈ 0、z 跨度=总高
        // （OpenSCAD 口径，后端 mesh.analyze 也按「min.z≈0 即贴平台」判定），
        // 而台面/网格/阴影都铺在 XZ 平面上。不转的话真实模型是「躺倒」的
        // ——之前的 mock 恰好用的 Y 向上坐标，所以验收截图里看不出来。
        // 旋转后再算包围盒，宽/深/高就能与世界轴一一对应，尺寸标注才不会标错轴。
        geometry.rotateX(-Math.PI / 2);
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

        const group = new THREE.Group();
        group.add(mesh, edges);
        // 建对象时就按**当前**线框状态定好可见性（判据与开关切换共用一份），
        // 否则开着线框新建的模型会先以实心闪一下，甚至停在实心状态。
        applyWireframe(group, wireframeRef.current);
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

        // ---- 尺寸标注：世界包围盒 = 几何包围盒 + 摆放偏移 ----
        const worldBox = box.clone().translate(group.position);
        annotationRef.current = buildOverallDimensions({
          min: [worldBox.min.x, worldBox.min.y, worldBox.min.z],
          max: [worldBox.max.x, worldBox.max.y, worldBox.max.z],
        });
        pickingRef.current = {
          mesh,
          edges,
          footprint: Math.max(size.x, size.y, size.z, 1),
        };
        // 换了模型就是换了对象，上一版的选中必须清掉
        selectedRef.current = null;
        setSelectedMm(null);
        setHasAnnotations(annotationRef.current.length > 0);
        rebuildDimsRef.current();
        rebuildLabelsRef.current();

        const position = geometry.getAttribute('position');
        onStats({
          // 旋转后世界 X=宽、Y=高、Z=深，与后端 bbox（x=宽、y=深、z=高）同一语义
          size: { width: size.x, depth: size.z, height: size.y },
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
  }, [projectId, revision, ready, onStats, onLoadingChange, onError, reloadNonce]);

  // ---- 线框 / 自动旋转 / 重置视角 ----
  useEffect(() => {
    // 异步加载 STL 的回调要按**此刻**的线框状态建对象，所以状态必须先进 ref。
    wireframeRef.current = wireframe;
    const core = coreRef.current;
    if (!core) return;
    applyWireframe(core.model, wireframe);
    // reloadNonce 也要在依赖里：模型重建（换项目 / 换版本 / 验收脚本强制重载）
    // 之后，这个 effect 必须**再跑一遍**把新对象的可见性纠正过来。
    // 少了它，新模型会一直停在建对象时那一刻的可见性 —— 而建对象发生在
    // 本 effect 之后，于是"线框开着却显示实心"。
  }, [wireframe, ready, projectId, revision, reloadNonce]);

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

  // ---- 尺寸标注开关 ----
  useEffect(() => {
    showDimsRef.current = showDimensions;
    if (!showDimensions) selectedRef.current = null; // 关掉标注不该留一个选不中的高亮
    clearSelectionRef.current();
    rebuildDimsRef.current();
    rebuildLabelsRef.current();
    setSelectedMm(null);
  }, [showDimensions]);

  return (
    <div className="viewer-root">
      <div className="viewer-canvas" ref={hostRef} />
      {/* 尺寸标签：命令式维护的 HTML 叠层，每帧只改 transform，不走 React 重渲染 */}
      <div className="viewer-dims" ref={layerRef} aria-hidden="true" />
      {showDimensions && hasAnnotations ? (
        <div className="viewer-dim-hint">
          {selectedMm !== null ? (
            <>
              <span className="dim-hint-value">已选边 {formatMm(selectedMm)} mm</span>
              <button
                type="button"
                className="dim-hint-clear"
                onClick={() => clearSelectionRef.current()}
              >
                取消选择
              </button>
            </>
          ) : (
            <span>点击模型上的棱线看该边长度</span>
          )}
        </div>
      ) : null}
      {/* 选中信息不能只靠画面上的浮标：给读屏器一份可读状态 */}
      <span className="sr-only" role="status">
        {selectedMm !== null ? `已选中一条棱线，长度 ${formatMm(selectedMm)} 毫米` : ''}
      </span>
    </div>
  );
}

/** 把一批线段端点包成 LineSegments（尺寸线、引出线都用它） */
function segmentsFrom(points: number[], material: THREE.Material): THREE.LineSegments {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3));
  const lines = new THREE.LineSegments(geometry, material);
  // 尺寸线要始终读得到，不被模型表面遮住
  lines.renderOrder = 3;
  lines.frustumCulled = false;
  return lines;
}

/**
 * 按线框开关设置「实体网格」与「描边」的可见性。
 *
 * 抽成函数是为了让**建对象时**与**开关变化时**走同一份判据 —— 两处各写一遍
 * 就会出现"创建时按一种规则、切换时按另一种规则"，模型重建后状态对不上
 * （这正是本轮修的缺陷的形状）。
 *
 * 线框开着：隐藏实体、显示描边（描边就是线框模式的替身）；
 * 线框关掉：显示实体、隐藏描边（否则实体上会叠一层棱线，像渲染坏了）。
 */
function applyWireframe(model: THREE.Object3D, wireframe: boolean): void {
  model.traverse((obj) => {
    if (obj.name === 'stl' && obj instanceof THREE.Mesh) obj.visible = !wireframe;
    if (obj.name === 'edges' && obj instanceof THREE.LineSegments) obj.visible = wireframe;
  });
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
