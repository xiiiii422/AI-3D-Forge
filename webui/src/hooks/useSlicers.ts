import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { api } from '../api/client';
import type { Slicer } from '../api/types';
import { toFriendlyError, type FriendlyError } from './errors';

/**
 * 切片软件列表（GET /api/slicers）的一份全局缓存。
 *
 * 为什么不再抄一个 Provider（打印机那边有现成范式）：全应用只有两处要看它 ——
 * 结果卡上的「送到切片软件」按钮、设置页的一行选择。为两处把 Provider 挂到树上，
 * 换来的只是多一层文件和多一段样板。这里用一个模块级 store + useSyncExternalStore：
 * 两处永远读到同一份数据（设置页换了默认，结果卡的提示立刻跟着变），代码却只有百来行。
 *
 * 关键一点是**懒**：扫描要翻注册表、列几个安装目录（实测本机几十毫秒，机器不同时更久），
 * 所以它不在启动时发，而是等第一个真的需要它的界面挂载才发；成功后不再重复发
 * （后端自己也缓存着，只有点「重新检测」才 force）。
 */

export interface SlicersSnapshot {
  slicers: Slicer[];
  /** 用户指定的默认目标；null = 用检测到的第一个（与后端 resolve() 同一套规则） */
  defaultId: string | null;
  count: number;
  /** 我们认得哪些切片软件（没检测到时界面用来提示"装其中任意一个就行"） */
  recognizedNames: string[];
  loading: boolean;
  /** 至少成功拉到过一次：按钮区在此之前什么都不显示，避免闪一下 */
  loaded: boolean;
  error: FriendlyError | null;
}

const EMPTY: SlicersSnapshot = {
  slicers: [],
  defaultId: null,
  count: 0,
  recognizedNames: [],
  loading: false,
  loaded: false,
  error: null,
};

let snapshot: SlicersSnapshot = EMPTY;
const listeners = new Set<() => void>();
let inflight: Promise<void> | null = null;
/** 拉取失败后的重试冷却：没有它，每张结果卡挂载都会各自再发一次请求 */
let nextTryAt = 0;
const RETRY_COOLDOWN_MS = 30_000;

function emit(next: Partial<SlicersSnapshot>) {
  snapshot = { ...snapshot, ...next };
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot() {
  return snapshot;
}

/**
 * 拉一次列表。
 *
 * 两个参数不能合一个：`rescan` 是“让后端重扫磁盘”，`refresh` 是“别拿缓存糊我”。
 * 实测踩过：只拿 `rescan` 一个开关时，设置页改完默认项后调 `reload(false)`
 * 被“已经加载过”这条懒加载规则直接短路，界面拿着旧列表显示，下拉框跳回上一个软件
 * —— 后端明明存住了，用户看到的是“改不动”。
 */
function load({ rescan = false, refresh = false }: { rescan?: boolean; refresh?: boolean } = {}): Promise<SlicersSnapshot> {
  if (inflight) return inflight.then(() => snapshot);
  if (!rescan && !refresh && (snapshot.loaded || Date.now() < nextTryAt)) return Promise.resolve(snapshot);
  emit({ loading: true });
  inflight = api
    .listSlicers({ rescan })
    .then((res) => {
      emit({
        slicers: res.slicers ?? [],
        defaultId: res.default_id ?? null,
        count: res.count ?? (res.slicers?.length ?? 0),
        recognizedNames: res.recognized_names ?? [],
        loaded: true,
        error: null,
      });
    })
    .catch((err) => {
      nextTryAt = Date.now() + RETRY_COOLDOWN_MS;
      emit({ loaded: false, error: toFriendlyError(err) });
    })
    .finally(() => {
      inflight = null;
      emit({ loading: false });
    });
  // 返回“拉完之后的那份快照”而不是 void：调用方（设置页的重新检测、手动指定）
  // 要拿新数字写提示，拿本次渲染里的旧值会把用户骗一次。
  return inflight.then(() => snapshot);
}

export interface SlicerStore extends SlicersSnapshot {
  /** 这次送出会用哪个软件（后端 resolve() 的同一条规则：默认项，否则第一个） */
  target: Slicer | null;
  /**
   * 重新拉一次（总是真发请求）。rescan=true 连后端缓存也打掉，重扫注册表与目录。
   * 改完设置、手动指定了主程序、送出报错后都要走它，否则界面会拿着旧列表骗用户。
   */
  reload: (rescan?: boolean) => Promise<SlicersSnapshot>;
}

export function useSlicers(): SlicerStore {
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    // 不传 rescan 也不传 refresh：挂载走懒加载规则（拉过就不再发）
    void load();
  }, []);

  const reload = useCallback(
    (rescan = false) => load({ rescan, refresh: true }),
    [],
  );

  return {
    ...snap,
    target: snap.slicers.find((s) => s.id === snap.defaultId) ?? snap.slicers[0] ?? null,
    reload,
  };
}

/** 我们认识的软件里，界面文案最多报几个名字（识别表可以宽，给用户看的清单要窄） */
export const RECOGNIZED_NAMES_SHOWN = 4;
