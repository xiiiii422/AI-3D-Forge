import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, MODE_PRESETS } from '../api/client';
import type { OpenScadInfo } from '../api/types';
import { Accordion } from '../components/Accordion';
import { Button } from '../components/Button';
import { Card } from '../components/Card';
import {
  IconCheck,
  IconCube,
  IconEye,
  IconEyeOff,
  IconFolder,
  IconInfo,
  IconKey,
  IconLayers,
  IconNozzle,
  IconPrinter,
  IconRefresh,
  IconSparkle,
  IconWrench,
} from '../components/Icons';
import { PrinterPicker } from '../components/PrinterPicker';
import {
  PrinterChecklist,
  PrinterKnowledge,
  PrinterManager,
} from '../components/PrinterManager';
import { ProgressBar } from '../components/ProgressBar';
import { StatusDot } from '../components/StatusDot';
import { useToast } from '../components/Toast';
import { toFriendlyError } from '../hooks/errors';
import { useInstallStream } from '../hooks/useInstallStream';
import { num, printerTitle, trimNum, usePrinters } from '../hooks/usePrinters';
import { RECOGNIZED_NAMES_SHOWN, useSlicers } from '../hooks/useSlicers';
import { useSettings } from '../hooks/useSettings';
import {
  TRANSPARENCY_DEFAULT,
  TRANSPARENCY_MAX,
  TRANSPARENCY_MIN,
  useAppearance,
  type AppearanceChoice,
} from '../hooks/useAppearance';
import { useTheme, type ThemeChoice } from '../hooks/useTheme';

// 使用模式预设由 api/client.ts 的 MODE_PRESETS 统一维护（顶栏与设置页共用，避免两处清单不一致）。
// 智谱 GLM 的视觉/错误码/输出预算由后端按 base_url 自动适配。

const DEEPSEEK_MODELS = [
  { value: 'deepseek-chat', label: 'deepseek-chat（通用，速度快）' },
  { value: 'deepseek-reasoner', label: 'deepseek-reasoner（推理更强，稍慢）' },
];

const GLM_MODELS = [
  { value: 'glm-4.7-flash', label: 'glm-4.7-flash（免费档，快）' },
  { value: 'glm-4-flash', label: 'glm-4-flash（免费档，非思考）' },
  { value: 'glm-4.5-flash', label: 'glm-4.5-flash（思考模型，更强）' },
];

// 模型下拉随服务地址联动，防止「地址是智谱、模型选 deepseek」这类错配。
function modelsForBaseUrl(url: string) {
  const u = (url || '').toLowerCase();
  if (u.includes('bigmodel.cn') || u.includes('zhipu')) return GLM_MODELS;
  if (u.includes('deepseek')) return DEEPSEEK_MODELS;
  return [...DEEPSEEK_MODELS, ...GLM_MODELS];
}

const THEMES: { value: ThemeChoice; label: string }[] = [
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
  { value: 'system', label: '跟随系统' },
];

const MATERIALS: { value: AppearanceChoice; label: string }[] = [
  { value: 'solid', label: '普通' },
  { value: 'glass', label: '液态玻璃' },
];

export default function Settings() {
  const navigate = useNavigate();
  const toast = useToast();
  const { settings, loading, saving, save } = useSettings();
  const { theme, setTheme } = useTheme();
  const { appearance, transparency, preview } = useAppearance();
  const printers = usePrinters();

  const [pickerOpen, setPickerOpen] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);

  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [replacingKey, setReplacingKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testText, setTestText] = useState<{ ok: boolean; text: string } | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('deepseek-chat');

  const [engine, setEngine] = useState<OpenScadInfo | null>(null);
  const [engineBusy, setEngineBusy] = useState(false);
  const [slicerBusy, setSlicerBusy] = useState(false);
  const slicers = useSlicers();
  const [testing3d, setTesting3d] = useState(false);
  const [installJobId, setInstallJobId] = useState<string | null>(null);
  const install = useInstallStream(installJobId);

  // 后端设置到达后同步到本地表单
  useEffect(() => {
    if (!settings) return;
    setBaseUrl(settings.base_url);
    setModel(settings.model);
    setTheme(settings.theme, { persist: true });
    // 材质与透明度：后端存的是权威值（包括它夹过的透明度），以它为准
    preview({
      appearance: settings.appearance === 'glass' ? 'glass' : 'solid',
      transparency: settings.glass_transparency,
    });
  }, [settings, setTheme, preview]);

  const modelOptions = modelsForBaseUrl(baseUrl);

  const detectEngine = useCallback(async () => {
    setEngineBusy(true);
    try {
      const info = await api.openscadDetect();
      setEngine(info);
      return info;
    } catch (err) {
      toast.error(toFriendlyError(err).message);
      return null;
    } finally {
      setEngineBusy(false);
    }
  }, [toast]);

  useEffect(() => {
    void detectEngine();
  }, [detectEngine]);

  useEffect(() => {
    if (install.done) {
      setEngine(install.done.info);
      toast.ok('3D 引擎已安装并配置完成。');
      setInstallJobId(null);
    }
  }, [install.done, toast]);

  const handleSaveKey = async () => {
    const key = apiKey.trim();
    if (!key) {
      toast.warn('请先粘贴 API Key。');
      return;
    }
    const saved = await save({ api_key: key });
    if (saved) {
      setApiKey('');
      setReplacingKey(false);
      setShowKey(false);
      toast.ok('API Key 已保存。');
    } else {
      toast.error('保存失败，请稍后重试。');
    }
  };

  const handleClearKey = async () => {
    const saved = await save({ api_key: '' });
    if (saved) {
      setReplacingKey(false);
      toast.ok('已清除 API Key。');
    }
  };

  const handleTestKey = async () => {
    setTesting(true);
    setTestText(null);
    try {
      const res = await api.testApiKey(apiKey.trim() ? apiKey.trim() : undefined);
      // 后端 message 已自带「（耗时 X 毫秒）」，这里再拼一次会出现重复耗时
      // （实测界面显示「…（耗时 49224 毫秒）。（49224 毫秒）」）。
      setTestText({
        ok: res.ok,
        text: res.message,
      });
      if (res.ok) toast.ok('AI 服务连接正常。');
    } catch (err) {
      setTestText({ ok: false, text: toFriendlyError(err).message });
    } finally {
      setTesting(false);
    }
  };

  const handleBrowseEngine = async () => {
    setEngineBusy(true);
    try {
      const { path } = await api.openscadBrowse();
      if (!path) {
        toast.show('没有选择文件。');
        return;
      }
      const info = await api.openscadLocate(path);
      setEngine(info);
      if (info.found && info.cli_ok) toast.ok('3D 引擎已配置完成。');
      else toast.warn(info.detail || '这个文件暂时不可用。');
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    } finally {
      setEngineBusy(false);
    }
  };

  const handleTestEngine = async () => {
    setTesting3d(true);
    try {
      const res = await api.openscadTest();
      if (res.ok) toast.ok(`${res.message}（${res.duration_ms} 毫秒）`);
      else toast.error(res.message);
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    } finally {
      setTesting3d(false);
    }
  };

  const handleStartInstall = async () => {
    try {
      const { job_id } = await api.openscadInstall('auto');
      setInstallJobId(job_id);
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    }
  };

  /**
   * 换默认切片软件：只存 id（路径每次实扫，免得住址变了存下一份假的）。
   * 存完必须把列表读回来而不是本地改状态：后端才是“这个 id 现在还在不在”的权威。
   */
  const chooseSlicer = async (id: string) => {
    const next = slicers.slicers.find((s) => s.id === id);
    setSlicerBusy(true);
    const saved = await save({ default_slicer: id });
    if (saved) {
      await slicers.reload();
      toast.ok(next ? `以后点「送到切片软件」会交给 ${next.name}。` : '设置已保存。');
    } else {
      toast.error('默认切片软件没存住，请重试。');
    }
    setSlicerBusy(false);
  };

  const rescanSlicers = async () => {
    setSlicerBusy(true);
    const snap = await slicers.reload(true);
    setSlicerBusy(false);
    if (snap.error) toast.error(snap.error.message);
    else if (snap.count > 0) toast.ok(`检测到 ${snap.count} 个切片软件：${snap.slicers.map((s) => s.name).join('、')}。`);
    else toast.warn('还是没检测到切片软件。装一个再重检，或点下面的「手动指定主程序」。');
  };

  /** 自动检测总会有漏网的机器（绿色版、装在冷门目录）：让用户自己指出 exe */
  const pickSlicer = async () => {
    setSlicerBusy(true);
    try {
      const res = await api.pickSlicer();
      if (res.status === 'added') {
        const snap = await slicers.reload();
        toast.ok(`已加入手动指定的主程序，现在共找到 ${snap.count} 个切片软件。`);
      } else if (res.status === 'canceled') {
        toast.show('没有选择文件。');
      } else {
        toast.warn('当前环境弹不出系统选择框，请在桌面版里再点一次。');
      }
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    } finally {
      setSlicerBusy(false);
    }
  };

  const openFolder = async (kind: 'data' | 'projects') => {
    try {
      await api.openFolder({ kind });
    } catch (err) {
      toast.error(toFriendlyError(err).message);
    }
  };

  const patch = async (body: Parameters<typeof save>[0], okText: string) => {
    const saved = await save(body);
    if (saved) toast.ok(okText);
    else toast.error('保存失败，请稍后重试。');
  };

  /**
   * 选材质：先在本地立刻上屏（整个界面实时变化），再写后端。
   * 存失败时必须说清楚：本地已经改了，不提醒的话用户重启后会看到
   * “莫名弹回普通”，而且不知道是哪一步没存住。
   */
  const chooseMaterial = (next: AppearanceChoice) => {
    if (next === appearance) return;
    preview({ appearance: next });
    void patch(
      { appearance: next },
      next === 'glass' ? '已启用液态玻璃，可以拖下面的滑块调透明度。' : '已换回普通外观。',
    );
  };

  /**
   * 透明度：拖动过程中只改本地（实时看效果），松手/键盘改完/失焦才写后端。
   * 不做“每一下 input 都 PUT”：一个拖动几十次请求，只会把设置接口刷成垃圾堆。
   */
  const commitTransparency = (value: number) => {
    void save({ glass_transparency: value }).then((saved) => {
      if (!saved) toast.error('透明度没存住，请重试。');
    });
  };

  if (loading && !settings) {
    return (
      <div className="page">
        <div className="stack">
          {[0, 1, 2].map((i) => (
            <Card key={i}>
              <div className="skeleton" style={{ height: 14, width: '28%', marginBottom: 14 }} />
              <div className="skeleton" style={{ height: 38, width: '100%', marginBottom: 10 }} />
              <div className="skeleton" style={{ height: 38, width: '68%' }} />
            </Card>
          ))}
        </div>
      </div>
    );
  }

  const keySet = settings?.api_key_set ?? false;
  const engineReady = Boolean(engine?.found && engine.cli_ok);

  return (
    <div className="page">
      <div className="page-head">
        <h1>设置</h1>
        <p>所有配置都保存在这台电脑上，不会上传。</p>
      </div>

      <div className="stack">
        {/* ---------------- 打印机 ---------------- */}
        <Card>
          <div className="card-head">
            <div>
              <h2>打印机</h2>
              <div className="card-hint">
                决定模型能打多大、壁厚留多少、要不要加支撑。换机后重新生成即可适配。
              </div>
            </div>
            <StatusDot
              status={printers.selected ? 'ok' : printers.error ? 'fail' : 'checking'}
            />
          </div>

          {printers.selected ? (
            <>
              <div className="printer-current">
                <span className="printer-current-icon">
                  <IconPrinter size={20} />
                </span>
                <div className="grow">
                  <div className="printer-current-name">
                    <span className="nowrap" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {printerTitle(printers.selected)}
                    </span>
                    <span className={printers.selected.builtin ? 'tag' : 'tag tag-ok'}>
                      {printers.selected.builtin ? '内置机型' : '自定义'}
                    </span>
                  </div>
                  <div className="printer-current-spec">
                    {printers.selected.nozzle_mm > 0
                      ? `${trimNum(printers.selected.nozzle_mm, 2)}mm 喷嘴`
                      : '光固化（无喷嘴）'}{' '}
                    · {num(printers.selected.build_x)}×{num(printers.selected.build_y)}×
                    {num(printers.selected.build_z)}mm ·{' '}
                    {printers.selected.enclosure ? '封闭腔体' : '开放机身'}
                  </div>
                </div>
                <div className="row" style={{ flexWrap: 'wrap' }}>
                  <Button
                    icon={<IconNozzle size={15} />}
                    onClick={() => setPickerOpen(true)}
                    loading={printers.selecting}
                  >
                    更换打印机
                  </Button>
                  <Button icon={<IconPrinter size={15} />} onClick={() => setManagerOpen(true)}>
                    管理机型
                  </Button>
                </div>
              </div>

              <div className="setting-block">
                <div className="field">
                  <label className="field-label" htmlFor="printer-material">
                    材料
                  </label>
                  <select
                    id="printer-material"
                    className="select"
                    style={{ maxWidth: 260 }}
                    value={printers.selectedMaterial}
                    disabled={printers.selecting}
                    onChange={(e) => {
                      const next = e.target.value;
                      void printers.select(printers.selectedId, next).then((outcome) => {
                        if (outcome.ok) toast.ok(`材料已切换为 ${outcome.material}。`);
                        else toast.error(outcome.message);
                      });
                    }}
                  >
                    {printers.selected.materials.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                  <span className="field-hint">
                    材料会影响壁厚、桥接与支撑建议。这台机器支持{' '}
                    {printers.selected.materials.join('、')}。
                  </span>
                </div>
              </div>

              <div className="setting-block">
                <div className="field">
                  <span className="field-label">这台机器的设计要求</span>
                  <PrinterChecklist items={printers.selected.checklist} />
                  <span className="field-hint">
                    这些规则会写进 AI 的设计要求，也会用于检查生成的模型是否打得出。
                  </span>
                </div>
              </div>

              <PrinterKnowledge />
            </>
          ) : (
            <div className="stack-sm">
              <div className="skeleton" style={{ height: 46, width: '100%' }} />
              <div className="skeleton" style={{ height: 46, width: '72%' }} />
              {printers.error ? (
                <div className="notice">
                  <span>{printers.error.message}</span>
                </div>
              ) : null}
            </div>
          )}
        </Card>

        {/* ---------------- AI 服务 ---------------- */}
        <Card>
          <div className="card-head">
            <div>
              <h2>AI 服务</h2>
              <div className="card-hint">负责把你的描述变成模型代码。</div>
            </div>
            <StatusDot status={keySet ? 'ok' : 'warn'} />
          </div>

          <div className="setting-block">
            <div className="field">
              <span className="field-label">API Key</span>
              {keySet && !replacingKey ? (
                <div className="row-between">
                  <span className="row" style={{ gap: 8, minWidth: 0 }}>
                    <IconKey size={16} style={{ color: 'var(--text-3)' }} />
                    <code className="mono">{settings?.api_key_masked ?? '已保存'}</code>
                    <span className="tag">
                      {settings?.api_key_store === 'credential_manager'
                        ? 'Windows 凭据管理器'
                        : settings?.api_key_store === 'env'
                          ? '来自环境变量'
                          : '配置文件'}
                    </span>
                  </span>
                  <span className="row" style={{ gap: 6 }}>
                    <Button size="sm" onClick={() => setReplacingKey(true)}>
                      更换
                    </Button>
                    <Button size="sm" variant="danger" onClick={() => void handleClearKey()} loading={saving}>
                      清除
                    </Button>
                  </span>
                </div>
              ) : (
                <>
                  <div className="input-group">
                    <div className="input-affix">
                      <input
                        className="input input-mono"
                        type={showKey ? 'text' : 'password'}
                        autoComplete="off"
                        spellCheck={false}
                        placeholder="sk-..."
                        aria-label="API Key"
                        value={apiKey}
                        onChange={(e) => {
                          setApiKey(e.target.value);
                          setTestText(null);
                        }}
                      />
                      <button
                        type="button"
                        className="input-affix-btn"
                        aria-label={showKey ? '隐藏 API Key' : '显示 API Key'}
                        onClick={() => setShowKey((v) => !v)}
                      >
                        {showKey ? <IconEyeOff size={16} /> : <IconEye size={16} />}
                      </button>
                    </div>
                    <Button variant="primary" onClick={() => void handleSaveKey()} loading={saving}>
                      保存
                    </Button>
                    {keySet ? (
                      <Button
                        variant="ghost"
                        onClick={() => {
                          setReplacingKey(false);
                          setApiKey('');
                        }}
                      >
                        取消
                      </Button>
                    ) : null}
                  </div>
                  <span className="field-hint">
                    保存后界面只显示掩码，不会明文回显。Key 存放在 Windows 凭据管理器。
                  </span>
                </>
              )}
            </div>
          </div>

          <div className="setting-block">
            <div className="field">
              <span className="field-label">使用模式</span>
              <div className="input-group">
                {MODE_PRESETS.map((p) => {
                  const active =
                    settings?.base_url === p.base_url &&
                    settings?.model === p.model &&
                    (settings?.economy_model ?? '') === p.economy_model &&
                    (settings?.economy_base_url ?? '') === p.economy_base_url;
                  return (
                    <Button
                      key={p.id}
                      variant={active ? 'primary' : 'default'}
                      loading={saving}
                      onClick={() => {
                        setBaseUrl(p.base_url);
                        setModel(p.model);
                        void patch(
                          {
                            base_url: p.base_url,
                            model: p.model,
                            economy_base_url: p.economy_base_url,
                            economy_model: p.economy_model,
                          },
                          `已切换到「${p.label}」。`,
                        );
                      }}
                    >
                      {p.label}
                    </Button>
                  );
                })}
              </div>
              <span className="field-hint">
                免费·GLM Flash：全部走智谱免费档；DeepSeek·付费均衡：全部走 DeepSeek（单价低）；
                混合·性价比：生成/修改用 DeepSeek 付费档，规格解析/打印性评审/复核用智谱免费档。
                切换模式前请确保两家服务商的 API Key 都已保存（Key 按服务商分开存储，互不覆盖）。
                {settings?.economy_model
                  ? `当前经济档：${settings.economy_model}。`
                  : ''}
              </span>
            </div>
          </div>

          <div className="setting-block">
            <div className="field">
              <label className="field-label" htmlFor="base-url">
                服务地址
              </label>
              <div className="input-group">
                <input
                  id="base-url"
                  className="input input-mono"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.deepseek.com"
                />
                <Button
                  onClick={() => void patch({ base_url: baseUrl.trim() }, '服务地址已保存。')}
                  loading={saving}
                  disabled={!baseUrl.trim() || baseUrl.trim() === settings?.base_url}
                >
                  保存
                </Button>
              </div>
              <span className="field-hint">默认使用官方地址，一般不需要修改。</span>
            </div>
          </div>

          <div className="setting-block">
            <div className="field">
              <label className="field-label" htmlFor="model-select">
                模型
              </label>
              <div className="input-group">
                <select
                  id="model-select"
                  className="select"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                >
                  {modelOptions.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                  {modelOptions.every((m) => m.value !== model) ? (
                    <option value={model}>{model}</option>
                  ) : null}
                </select>
                <Button
                  onClick={() => void patch({ model }, '模型已保存。')}
                  loading={saving}
                  disabled={model === settings?.model}
                >
                  保存
                </Button>
                <Button
                  icon={<IconWrench size={15} />}
                  loading={testing}
                  disabled={!keySet && !apiKey.trim()}
                  onClick={() => void handleTestKey()}
                >
                  测试连接
                </Button>
              </div>
              {testText ? (
                <div
                  className={testText.ok ? 'notice notice-ok' : 'notice'}
                  style={{ marginTop: 8 }}
                  role="status"
                >
                  {testText.ok ? <IconCheck size={15} /> : <StatusDot status="fail" />}
                  <span>{testText.text}</span>
                </div>
              ) : null}
            </div>
          </div>
        </Card>

        {/* ---------------- 3D 引擎 ---------------- */}
        <Card>
          <div className="card-head">
            <div>
              <h2>3D 引擎</h2>
              <div className="card-hint">把设计稿编译成可打印的模型文件。</div>
            </div>
            <StatusDot status={engineReady ? 'ok' : engine ? 'warn' : 'checking'} />
          </div>

          <div className="field">
            <span className="field-label">引擎位置</span>
            <div className="path-box">
              <IconCube size={16} style={{ color: 'var(--text-3)', flex: 'none' }} />
              <code>
                {engine?.path ?? (engine?.found ? '已找到，但无法运行' : '尚未检测到 3D 引擎')}
              </code>
              {engine?.version ? <span className="tag">版本 {engine.version}</span> : null}
            </div>
            {engine?.detail ? <span className="field-hint">{engine.detail}</span> : null}
          </div>

          <div className="row" style={{ marginTop: 'var(--sp-4)', flexWrap: 'wrap' }}>
            <Button
              icon={<IconRefresh size={15} />}
              loading={engineBusy}
              onClick={() => void detectEngine()}
            >
              自动检测
            </Button>
            <Button loading={engineBusy} onClick={() => void handleBrowseEngine()}>
              浏览…
            </Button>
            <Button
              icon={<IconSparkle size={15} />}
              variant="brand"
              loading={install.running}
              onClick={() => void handleStartInstall()}
            >
              自动安装
            </Button>
            <Button
              icon={<IconWrench size={15} />}
              loading={testing3d}
              disabled={!engineReady}
              onClick={() => void handleTestEngine()}
            >
              运行测试
            </Button>
          </div>

          {install.running ? (
            <div style={{ marginTop: 'var(--sp-4)' }}>
              <ProgressBar
                percent={install.percent}
                label={install.message || install.stageLabel}
                value={install.percent === null ? undefined : `${Math.round(install.percent)}%`}
              />
            </div>
          ) : null}

          {install.error ? (
            <div className="err" style={{ marginTop: 'var(--sp-4)' }}>
              <div className="err-title">
                <StatusDot status="fail" />
                自动安装没有完成
              </div>
              <div>{install.error.message}</div>
              {install.error.manual_steps?.length ? (
                <ol className="field-hint" style={{ paddingLeft: 20 }}>
                  {install.error.manual_steps.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ol>
              ) : null}
              <div className="row">
                <Button size="sm" onClick={() => navigate('/welcome')}>
                  打开安装向导
                </Button>
              </div>
            </div>
          ) : null}

          {engine && !engineReady && engine.candidates.length > 0 ? (
            <div style={{ marginTop: 'var(--sp-4)' }}>
              <Accordion title={`发现 ${engine.candidates.length} 个可用的引擎副本`}>
                <div className="dl">
                  {engine.candidates.map((c) => (
                    <div key={c.path} style={{ display: 'contents' }}>
                      <dt>{c.version ?? '版本未知'}</dt>
                      <dd>{c.path}</dd>
                    </div>
                  ))}
                </div>
              </Accordion>
            </div>
          ) : null}
        </Card>

        {/* ---------------- 切片软件 ---------------- */}
        <Card>
          <div className="card-head">
            <div>
              <h2>切片软件</h2>
              <div className="card-hint">模型生成后可以直接送进去切片、打印，不用自己找文件。</div>
            </div>
            <StatusDot status={slicers.count > 0 ? 'ok' : slicers.loaded ? 'warn' : 'checking'} />
          </div>

          {slicers.count > 0 ? (
            <div className="field">
              <label className="field-label" htmlFor="slicer-default">
                默认送到
              </label>
              <select
                id="slicer-default"
                className="select"
                style={{ maxWidth: 320 }}
                value={slicers.target?.id ?? ''}
                disabled={slicerBusy}
                onChange={(e) => void chooseSlicer(e.target.value)}
              >
                {slicers.slicers.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                    {s.version ? `（${s.version}）` : ''}
                  </option>
                ))}
              </select>
              <span className="field-hint">
                {slicers.target
                  ? `从${slicers.target.source_label}找到 · ${slicers.target.exe}`
                  : ''}
                {slicers.count > 1 ? `（共检测到 ${slicers.count} 个，换默认目标只需要改这里）` : ''}
              </span>
            </div>
          ) : slicers.loaded ? (
            <div className="notice">
              <IconInfo size={16} />
              <span>
                这台电脑上没检测到切片软件。装上
                {slicers.recognizedNames.slice(0, RECOGNIZED_NAMES_SHOWN).join('、')}
                其中任意一个就能一键送过去；绿色版或装在别处，可以点下面的「手动指定主程序」。
              </span>
            </div>
          ) : (
            <div className="stack-sm">
              <div className="skeleton" style={{ height: 38, width: '52%' }} />
              {slicers.error ? <span className="field-hint">{slicers.error.message}</span> : null}
            </div>
          )}

          <div className="row" style={{ marginTop: 'var(--sp-4)', flexWrap: 'wrap' }}>
            <Button
              icon={<IconRefresh size={15} />}
              loading={slicerBusy}
              onClick={() => void rescanSlicers()}
            >
              重新检测
            </Button>
            <Button
              icon={<IconLayers size={15} />}
              loading={slicerBusy}
              onClick={() => void pickSlicer()}
            >
              手动指定主程序…
            </Button>
          </div>
        </Card>

        {/* ---------------- 文件位置 ---------------- */}
        <Card>
          <div className="card-head">
            <div>
              <h2>文件位置</h2>
              <div className="card-hint">你的项目和数据都保存在用户目录，不在程序安装目录里。</div>
            </div>
          </div>

          <div className="notice" style={{ marginBottom: 'var(--sp-4)' }}>
            <IconInfo size={16} />
            <span>
              所有模型与配置都保存在你的 Windows 用户目录下。卸载或升级软件不会删除这些文件，也不会写入
              Program Files。
            </span>
          </div>

          <div className="stack-sm">
            <div className="field">
              <span className="field-label">项目文件</span>
              <div className="path-box">
                <IconFolder size={16} style={{ color: 'var(--text-3)', flex: 'none' }} />
                <code>{settings?.projects_dir ?? '—'}</code>
                <Button size="sm" onClick={() => void openFolder('projects')}>
                  打开文件夹
                </Button>
              </div>
              <span className="field-hint">每个项目一个文件夹，包含模型源码、STL 文件和历史版本。</span>
            </div>

            <div className="field">
              <span className="field-label">配置与缓存</span>
              <div className="path-box">
                <IconFolder size={16} style={{ color: 'var(--text-3)', flex: 'none' }} />
                <code>{settings?.data_dir ?? '—'}</code>
                <Button size="sm" onClick={() => void openFolder('data')}>
                  打开文件夹
                </Button>
              </div>
              <span className="field-hint">
                存放设置与缓存，可以安全清理缓存部分。日志不在这里，它在：
                <code>{settings?.log_dir ?? '—'}</code>
              </span>
            </div>
          </div>
        </Card>

        {/* ---------------- 行为 ---------------- */}
        <Card>
          <div className="card-head">
            <div>
              <h2>行为</h2>
              <div className="card-hint">按你的习惯调整。</div>
            </div>
          </div>

          <div className="switch-row">
            <div>
              <div className="field-label">保留历史模型文件</div>
              <div className="field-hint">
                每次生成都保留一份 STL，方便回退到旧版本。关闭后只保留最新版本，更省磁盘。
              </div>
            </div>
            <button
              type="button"
              role="switch"
              className="switch"
              aria-checked={settings?.keep_stl_history ?? true}
              aria-label="保留历史模型文件"
              disabled={saving}
              onClick={() =>
                void patch(
                  { keep_stl_history: !(settings?.keep_stl_history ?? true) },
                  '设置已保存。',
                )
              }
            />
          </div>

          <div className="switch-row">
            <div>
              <div className="field-label">缺少 3D 引擎时自动安装</div>
              <div className="field-hint">
                生成模型前如果发现 3D 引擎缺失，自动从官方源安装免安装版，无需管理员权限。
              </div>
            </div>
            <button
              type="button"
              role="switch"
              className="switch"
              aria-checked={settings?.auto_install_openscad ?? true}
              aria-label="缺少 3D 引擎时自动安装"
              disabled={saving}
              onClick={() =>
                void patch(
                  { auto_install_openscad: !(settings?.auto_install_openscad ?? true) },
                  '设置已保存。',
                )
              }
            />
          </div>

          <div className="switch-row">
            <div>
              <div className="field-label">生成前先确认「我对需求的理解」</div>
              <div className="field-hint">
                需求里给了具体数字时，先把整理好的尺寸规格交给你过目：可以改数字、
                补一句说明，确认后才开始设计。这是避免「做出来不是你要的」最有效的一步。
              </div>
            </div>
            <button
              type="button"
              role="switch"
              className="switch"
              aria-checked={settings?.confirm_spec ?? true}
              aria-label="生成前先确认我对需求的理解"
              disabled={saving}
              onClick={() =>
                void patch(
                  { confirm_spec: !(settings?.confirm_spec ?? true) },
                  '设置已保存。',
                )
              }
            />
          </div>

          <div className="switch-row">
            <div>
              <div className="field-label">生成前说明目标机型</div>
              <div className="field-hint">
                开始设计前在对话里说一遍这次按哪台机器、哪种材料来做，方便你及时发现选错了机型。
              </div>
            </div>
            <button
              type="button"
              role="switch"
              className="switch"
              aria-checked={settings?.announce_in_chat ?? true}
              aria-label="生成前说明目标机型"
              disabled={saving}
              onClick={() =>
                void patch(
                  { announce_in_chat: !(settings?.announce_in_chat ?? true) },
                  '设置已保存。',
                )
              }
            />
          </div>

        </Card>

        {/* ---------------- 外观 ----------------
            主题/材质/透明度以前寄在「行为」卡里：不是行为语义，用户找不到，
            文档里写的「设置 → 外观」也无从对应。 */}
        <Card>
          <div className="card-head">
            <div>
              <h2>外观</h2>
              <div className="card-hint">主题与界面材质。改动立即生效，不用重启。</div>
            </div>
          </div>

          <div className="switch-row">
            <div>
              <div className="field-label">外观主题</div>
              <div className="field-hint">浅色为默认；深色适合夜间使用。</div>
            </div>
            <div className="seg" role="group" aria-label="外观主题">
              {THEMES.map((t) => (
                <button
                  key={t.value}
                  type="button"
                  aria-pressed={theme === t.value}
                  onClick={() => {
                    setTheme(t.value);
                    void patch({ theme: t.value }, '外观已切换。');
                  }}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>

          <div className="switch-row switch-row-stack">
            <div>
              <div className="field-label">界面材质</div>
              <div className="field-hint">
                普通为实底面板；液态玻璃会让面板变半透并模糊背面的内容，
                顶栏、侧栏、卡片都会跟着变。改动立即生效，不用重启。
              </div>
            </div>
            <div className="seg" role="group" aria-label="界面材质">
              {MATERIALS.map((m) => (
                <button
                  key={m.value}
                  type="button"
                  aria-pressed={appearance === m.value}
                  disabled={saving}
                  onClick={() => chooseMaterial(m.value)}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          {/* 选液态玻璃才弹出来：只选「普通」时不需要看到透明度这个开关 */}
          {appearance === 'glass' ? (
            <div className="glass-tune">
              {/* 数字是给人看的装饰：读屏已经能从 aria-valuetext 拿到同一个值，
                  写进 label 会让名称变成「透明度 25%」这种又长又重复的东西。 */}
              <div className="glass-tune-head">
                <label className="glass-tune-label" htmlFor="glass-transparency">
                  透明度
                </label>
                <span className="glass-tune-value" aria-hidden="true">
                  {transparency}%
                </span>
              </div>
              <input
                id="glass-transparency"
                className="glass-range"
                type="range"
                min={TRANSPARENCY_MIN}
                max={TRANSPARENCY_MAX}
                step={1}
                value={transparency}
                aria-valuetext={`${transparency}%`}
                aria-describedby="glass-transparency-hint"
                onChange={(event) => preview({ transparency: Number(event.target.value) })}
                onPointerUp={(event) => commitTransparency(Number(event.currentTarget.value))}
                onKeyUp={(event) => commitTransparency(Number(event.currentTarget.value))}
                onBlur={(event) => commitTransparency(Number(event.currentTarget.value))}
              />
              <div className="field-hint" id="glass-transparency-hint">
                往右更通透，往左更结实。上限 {TRANSPARENCY_MAX}% 是量出来的，不是拍脑袋
                （脚本 webui/scripts/verify-glass-contrast.mjs）：到这个档位卡片上的字
                还读得动，但面板背后的内容会透上来 —— 实测面板底的明暗散布 25% 时 0.030、
                60% 时 0.072，再往右面板就不是一块干净的底了，看久了累。
                弹窗、菜单、提示条不跟着变透：它们背后压着整页文字，始终用厚玻璃，
                实测读数不随滑块变化。
              </div>
              <div className="glass-tune-foot">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    preview({ transparency: TRANSPARENCY_DEFAULT });
                    commitTransparency(TRANSPARENCY_DEFAULT);
                  }}
                >
                  恢复默认（{TRANSPARENCY_DEFAULT}%）
                </Button>
              </div>
            </div>
          ) : null}
        </Card>
      </div>

      {/* 机型选择器与机型管理：与工作台共用同一套组件 */}
      <PrinterPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        hint="选中后立即生效：之后生成的模型都会按这台机器设计。"
      />
      <PrinterManager open={managerOpen} onClose={() => setManagerOpen(false)} />
    </div>
  );
}
