import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { CheckItem, OpenScadInfo } from '../api/types';
import { Button } from '../components/Button';
import { CheckList, CheckListSkeleton } from '../components/CheckList';
import { IconArrowRight, IconCheck, IconExternal, IconEye, IconEyeOff, IconKey, IconLogo, IconNozzle, IconPrinter, IconRefresh, IconSparkle } from '../components/Icons';
import { ProgressBar } from '../components/ProgressBar';
import { StatusDot } from '../components/StatusDot';
import { useToast } from '../components/Toast';
import { useEnvReport } from '../hooks/useEnvReport';
import { useInstallStream } from '../hooks/useInstallStream';
import { num, printerTitle, usePrinters } from '../hooks/usePrinters';
import { useSettings } from '../hooks/useSettings';
import { toFriendlyError } from '../hooks/errors';

const STEPS = ['欢迎', '环境检测', '3D 引擎', 'AI 服务'] as const;

const DEEPSEEK_KEYS_URL = 'https://platform.deepseek.com/api_keys';

/** 向导里最常选的几台机器（其余机型请到「设置 → 打印机」里挑） */
const COMMON_PRINTERS: { id: string; label: string }[] = [
  { id: 'generic-220-04', label: '通用机型 220×220×250' },
  { id: 'bambu-x1c-04', label: 'Bambu Lab X1 Carbon' },
  { id: 'bambu-p1s-04', label: 'Bambu Lab P1S' },
  { id: 'prusa-mk4-04', label: 'Original Prusa MK4' },
  { id: 'creality-ender3-04', label: 'Creality Ender-3 系列' },
  { id: 'resin-generic-405', label: '光固化（树脂）打印机' },
];

/** 环境检测里与 3D 引擎相关的检查项，第 3 步要用 */
const ENGINE_IDS = new Set(['openscad', 'openscad_cli', 'openscad_version']);

export default function Welcome() {
  const navigate = useNavigate();
  const toast = useToast();
  const [step, setStep] = useState(0);

  const { report, loading, detecting, error, refresh } = useEnvReport();
  const { settings, save } = useSettings();

  // ---- 第 3 步：3D 引擎状态 ----
  const [engine, setEngine] = useState<OpenScadInfo | null>(null);
  const [engineLoading, setEngineLoading] = useState(false);
  const [installJobId, setInstallJobId] = useState<string | null>(null);
  const [locating, setLocating] = useState(false);
  const install = useInstallStream(installJobId);

  const detectEngine = useCallback(async () => {
    setEngineLoading(true);
    try {
      const info = await api.openscadDetect();
      setEngine(info);
      return info;
    } catch {
      setEngine(null);
      return null;
    } finally {
      setEngineLoading(false);
    }
  }, []);

  // 进入第 3 步时确认一次引擎状态
  useEffect(() => {
    if (step === 2 && !engine && !engineLoading) void detectEngine();
  }, [step, engine, engineLoading, detectEngine]);

  // 安装成功 → 刷新引擎信息与本地检查项
  useEffect(() => {
    if (!install.done) return;
    setEngine(install.done.info);
    void refresh();
  }, [install.done, refresh]);

  const engineReady = useMemo(() => {
    if (engine) return engine.found && engine.cli_ok;
    const item = report?.items.find((i) => i.id === 'openscad');
    return item?.status === 'ok';
  }, [engine, report]);

  const engineItem: CheckItem | undefined = report?.items.find((i) => i.id === 'openscad');

  // ---- 第 2 步：环境检测 ----
  const okCount = report?.ok_count ?? 0;
  const total = report?.total ?? 0;
  const ready = total > 0 && okCount >= total;
  const readinessTone = ready ? '' : okCount === 0 ? 'is-fail' : 'is-warn';
  const blockingItems = (report?.items ?? []).filter(
    (i) => i.status === 'fail' && !ENGINE_IDS.has(i.id),
  );

  // ---- 第 4 步：API Key ----
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [finishing, setFinishing] = useState(false);
  const keyInputRef = useRef<HTMLInputElement>(null);

  // ---- 第 4 步：机型（可选，不选就保持通用机型） ----
  const printers = usePrinters();
  const [choosingPrinter, setChoosingPrinter] = useState<string | null>(null);
  const [otherChosen, setOtherChosen] = useState(false);

  const commonPrinters = useMemo(
    () =>
      COMMON_PRINTERS.map((item) => ({
        ...item,
        printer: printers.printers.find((p) => p.id === item.id) ?? null,
      })).filter((item) => item.printer !== null),
    [printers.printers],
  );

  const handleChoosePrinter = async (printerId: string) => {
    const printer = printers.printers.find((p) => p.id === printerId);
    if (!printer) return;
    setChoosingPrinter(printerId);
    setOtherChosen(false);
    const outcome = await printers.select(printerId, printer.materials[0]);
    setChoosingPrinter(null);
    if (outcome.ok) toast.ok(`已选择 ${printerTitle(outcome.printer)}，之后生成的模型会按它设计。`);
    else toast.error(outcome.message);
  };

  const keyReady = (settings?.api_key_set ?? false) || apiKey.trim().length > 0;

  const handleStartInstall = async () => {
    try {
      const { job_id } = await api.openscadInstall('auto');
      setInstallJobId(job_id);
      setEngine(null);
    } catch (err) {
      const friendly = toFriendlyError(err);
      toast.error(friendly.message);
    }
  };

  const handleBrowse = async () => {
    setLocating(true);
    try {
      const { path } = await api.openscadBrowse();
      if (!path) {
        toast.show('没有选择文件。');
        return;
      }
      const info = await api.openscadLocate(path);
      setEngine(info);
      if (info.found && info.cli_ok) {
        toast.ok('3D 引擎已配置完成。');
        void refresh();
      } else {
        toast.warn(info.detail || '这个文件暂时不可用，请选择 openscad.exe。');
      }
    } catch (err) {
      const friendly = toFriendlyError(err);
      toast.error(friendly.message);
    } finally {
      setLocating(false);
    }
  };

  const handleTestKey = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await api.testApiKey(apiKey.trim() ? apiKey.trim() : undefined);
      setTestResult({
        ok: res.ok,
        text: res.message + (res.latency_ms ? `（${Math.round(res.latency_ms)} 毫秒）` : ''),
      });
      if (res.ok) toast.ok('连接成功，AI 服务可用。');
    } catch (err) {
      const friendly = toFriendlyError(err);
      setTestResult({ ok: false, text: friendly.message });
    } finally {
      setTesting(false);
    }
  };

  const handleOpenKeysPage = async () => {
    try {
      await api.openExternal(DEEPSEEK_KEYS_URL);
    } catch {
      // 后端不在（例如浏览器里预览）时退化为浏览器直接打开
      window.open(DEEPSEEK_KEYS_URL, '_blank', 'noopener');
    }
  };

  /** 保存 API Key（如果填了）→ 标记首次向导完成 → 进入工作台 */
  const finish = async (options: { skip?: boolean } = {}) => {
    setFinishing(true);
    try {
      const key = apiKey.trim();
      if (!options.skip && key) {
        const saved = await save({ api_key: key });
        if (!saved) throw new Error('保存 API Key 失败。');
      }
      try {
        await api.completeFirstRun();
      } catch {
        // 即使标记失败也不该把用户挡在门外
      }
      setApiKey('');
      navigate('/studio', { replace: true });
    } catch (err) {
      const friendly = toFriendlyError(err);
      toast.error(friendly.message);
    } finally {
      setFinishing(false);
    }
  };

  const settingsError = error;

  return (
    <div className="wizard">
      <nav className="wizard-steps" aria-label="配置步骤">
        {STEPS.map((label, index) => {
          const state = index === step ? 'is-active' : index < step ? 'is-done' : '';
          return (
            <span key={label} style={{ display: 'contents' }}>
              {index > 0 ? <span className="wizard-step-sep" /> : null}
              <span className={`wizard-step ${state}`} aria-current={index === step ? 'step' : undefined}>
                <span className="wizard-step-num">
                  {index < step ? <IconCheck size={12} /> : index + 1}
                </span>
                {label}
              </span>
            </span>
          );
        })}
      </nav>

      <div className="wizard-body">
        <div className={`wizard-panel${step === 1 ? ' wizard-panel-wide' : ''}`}>
          {/* ---------------- 1. 欢迎 ---------------- */}
          {step === 0 ? (
            <div className="hero">
              <span className="hero-mark">
                <IconLogo size={26} />
              </span>
              <h1>AI 3D Forge</h1>
              <p className="hero-lead">用一句话描述，得到可以直接打印的 3D 模型。</p>
              <div className="hero-points">
                <span className="tag">
                  <IconSparkle size={13} style={{ marginRight: 4 }} />
                  中文描述即可生成
                </span>
                <span className="tag">自动检查能否打印</span>
                <span className="tag">支持参数微调</span>
              </div>
              <div className="wizard-actions">
                <Button variant="primary" size="lg" onClick={() => setStep(1)} iconRight={<IconArrowRight size={16} />}>
                  开始配置
                </Button>
                <span className="muted small" style={{ marginLeft: 'var(--sp-3)' }}>
                  大约 1 分钟，随时可以跳过
                </span>
              </div>
            </div>
          ) : null}

          {/* ---------------- 2. 环境检测 ---------------- */}
          {step === 1 ? (
            <>
              <h1 className="wizard-title">环境检测</h1>
              <p className="wizard-sub">确认这台电脑具备生成 3D 模型所需的组件。</p>

              <div className="readiness">
                <div className={`readiness-num ${readinessTone}`}>
                  {report ? `${okCount}/${total}` : '—'}
                </div>
                <div className="grow">
                  <div style={{ fontWeight: 550 }}>
                    {report
                      ? ready
                        ? '环境就绪'
                        : `环境就绪 ${okCount}/${total}`
                      : '正在检测本机环境…'}
                  </div>
                  <div className="muted small">
                    {report
                      ? ready
                        ? '一切正常，可以开始设计。'
                        : '下面标黄或标红的项目需要处理，也可以先跳过。'
                      : '检测通常只需要几秒钟。'}
                  </div>
                </div>
                <Button
                  icon={<IconRefresh size={15} />}
                  loading={detecting}
                  onClick={() => void refresh()}
                >
                  重新检测
                </Button>
              </div>

              {loading && !report ? <CheckListSkeleton rows={7} /> : null}

              {report ? (
                <div className="card card-tight">
                  <CheckList items={report.items} showAction />
                </div>
              ) : null}

              {settingsError && !report ? (
                <div className="notice" style={{ marginTop: 'var(--sp-4)' }}>
                  <span>
                    <strong>暂时无法完成检测。</strong>
                    <br />
                    {settingsError.message} 可以继续下一步，稍后在「系统诊断」里重新检测。
                  </span>
                </div>
              ) : null}

              {blockingItems.length > 0 ? (
                <div className="notice" style={{ marginTop: 'var(--sp-4)' }}>
                  <span>
                    有 {blockingItems.length} 项未通过：
                    {blockingItems.map((i) => i.title).join('、')}。可以先跳过，不影响进入工作台。
                  </span>
                </div>
              ) : null}

              <div className="wizard-actions">
                <Button onClick={() => setStep(0)}>上一步</Button>
                <span className="spacer" />
                <Button variant="ghost" onClick={() => setStep(3)}>
                  跳过向导
                </Button>
                <Button variant="primary" onClick={() => setStep(2)} iconRight={<IconArrowRight size={16} />}>
                  下一步
                </Button>
              </div>
            </>
          ) : null}

          {/* ---------------- 3. 3D 引擎 ---------------- */}
          {step === 2 ? (
            <>
              <h1 className="wizard-title">3D 引擎</h1>
              <p className="wizard-sub">
                需要一个免费的 3D 引擎把设计稿转成模型文件。可以自动安装，也可以手动指定。
              </p>

              {engineLoading && !engine ? (
                <div className="card card-tight">
                  <div className="row">
                    <StatusDot status="checking" />
                    <span className="muted">正在查找本机是否已有 3D 引擎…</span>
                  </div>
                </div>
              ) : null}

              {/* 已就绪 */}
              {engineReady ? (
                <div className="notice notice-ok">
                  <IconCheck size={16} />
                  <span>
                    <strong>OpenSCAD 已安装并配置完成。</strong>
                    <br />
                    {engine?.path ?? engineItem?.detail ?? ''}
                    {engine?.version ? `（版本 ${engine.version}）` : ''}
                  </span>
                </div>
              ) : null}

              {/* 未安装：主操作 */}
              {!engineReady && !installJobId && !engineLoading ? (
                <div className="card">
                  <div className="row-between" style={{ alignItems: 'flex-start' }}>
                    <div>
                      <h2 style={{ fontSize: 'var(--fs-h3)' }}>还没有检测到 3D 引擎</h2>
                      <p className="muted small" style={{ marginTop: 4 }}>
                        {engineItem?.detail && engineItem.status !== 'checking'
                          ? engineItem.detail
                          : '安装过程会自动从官方源下载，约 25 MB。'}
                      </p>
                    </div>
                  </div>
                  <div className="wizard-actions" style={{ marginTop: 'var(--sp-4)' }}>
                    <Button variant="brand" icon={<IconSparkle size={15} />} onClick={() => void handleStartInstall()}>
                      自动安装 OpenSCAD
                    </Button>
                    <Button variant="ghost" loading={locating} onClick={() => void handleBrowse()}>
                      手动选择 openscad.exe
                    </Button>
                  </div>
                  <p className="field-hint" style={{ marginTop: 'var(--sp-3)' }}>
                    自动安装会先从官方源下载免安装版到你的用户目录，不需要管理员权限；如果系统要求，Windows
                    会弹出管理员授权提示。
                  </p>
                </div>
              ) : null}

              {/* 安装中 */}
              {installJobId && install.running ? (
                <div className="card">
                  <div className="row-between" style={{ marginBottom: 'var(--sp-3)' }}>
                    <span className="row" style={{ gap: 6 }}>
                      <StatusDot status="checking" />
                      <strong style={{ fontWeight: 550 }}>{install.stageLabel}</strong>
                    </span>
                    <span className="muted small">
                      {install.percent === null ? '正在准备…' : `${Math.round(install.percent)}%`}
                    </span>
                  </div>
                  {/* 单一进度条：阶段文案 + 实时百分比 */}
                  <ProgressBar percent={install.percent} label={install.message || '准备中…'} />
                  {install.logs.length > 0 ? (
                    <p className="field-hint" style={{ marginTop: 'var(--sp-3)' }}>
                      {install.logs[install.logs.length - 1]}
                    </p>
                  ) : null}
                </div>
              ) : null}

              {/* 安装失败：message + need_admin + manual_url + manual_steps */}
              {install.error ? (
                <div className="card" style={{ borderColor: 'var(--danger-soft)' }}>
                  <div className="err">
                    <div className="err-title">
                      <StatusDot status="fail" />
                      自动安装没有完成
                    </div>
                    <div>{install.error.message || '安装过程中出现问题。'}</div>
                    {install.error.need_admin ? (
                      <div className="err-hint">
                        这个步骤需要管理员权限：点击「重试」后，Windows
                        会弹出「你要允许此应用对你的设备进行更改吗？」的提示，请选择「是」。如果电脑由公司统一管理，可能需要联系管理员。
                      </div>
                    ) : null}
                  </div>

                  {install.error.manual_steps && install.error.manual_steps.length > 0 ? (
                    <div style={{ marginTop: 'var(--sp-4)' }}>
                      <div className="field-label">手动安装步骤</div>
                      <ol className="field-hint" style={{ paddingLeft: 20, marginTop: 6 }}>
                        {install.error.manual_steps.map((s) => (
                          <li key={s} style={{ marginBottom: 4 }}>
                            {s}
                          </li>
                        ))}
                      </ol>
                      {install.error.manual_url ? (
                        <a
                          href={install.error.manual_url}
                          onClick={(e) => {
                            e.preventDefault();
                            void api
                              .openExternal(install.error?.manual_url as string)
                              .catch(() =>
                                window.open(install.error?.manual_url as string, '_blank', 'noopener'),
                              );
                          }}
                        >
                          打开 OpenSCAD 官方下载页
                          <IconExternal size={13} style={{ marginLeft: 4, verticalAlign: -2 }} />
                        </a>
                      ) : null}
                    </div>
                  ) : null}

                  <div className="wizard-actions" style={{ marginTop: 'var(--sp-4)' }}>
                    <Button
                      variant="primary"
                      onClick={() => {
                        setInstallJobId(null);
                        void handleStartInstall();
                      }}
                    >
                      重试
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setInstallJobId(null);
                        void detectEngine();
                      }}
                    >
                      我已手动装好
                    </Button>
                    <Button variant="ghost" loading={locating} onClick={() => void handleBrowse()}>
                      手动选择 openscad.exe
                    </Button>
                  </div>
                </div>
              ) : null}

              {!engineReady && !installJobId ? (
                <details style={{ marginTop: 'var(--sp-4)' }}>
                  <summary className="muted small" style={{ cursor: 'pointer' }}>
                    找不到？这里是手动安装说明
                  </summary>
                  <ol className="field-hint" style={{ paddingLeft: 20, marginTop: 8 }}>
                    <li>打开 OpenSCAD 官方下载页，下载 Windows 安装包。</li>
                    <li>运行安装包，按默认选项完成安装。</li>
                    <li>回到这里点击「我已手动装好」，或直接「手动选择 openscad.exe」。</li>
                  </ol>
                </details>
              ) : null}

              <div className="wizard-actions">
                <Button onClick={() => setStep(1)}>上一步</Button>
                <span className="spacer" />
                <Button variant="ghost" onClick={() => setStep(3)}>
                  稍后再说
                </Button>
                <Button
                  variant="primary"
                  disabled={!engineReady}
                  onClick={() => setStep(3)}
                  iconRight={<IconArrowRight size={16} />}
                >
                  {engineReady ? '下一步' : '装好后继续'}
                </Button>
              </div>
            </>
          ) : null}

          {/* ---------------- 4. AI 服务 ---------------- */}
          {step === 3 ? (
            <>
              <h1 className="wizard-title">AI 服务</h1>
              <p className="wizard-sub">
                填入 DeepSeek 的 API Key，AI 才能把你的描述变成模型。Key 只保存在这台电脑上。
              </p>

              {settings?.api_key_set ? (
                <div className="notice notice-ok" style={{ marginBottom: 'var(--sp-4)' }}>
                  <IconCheck size={16} />
                  <span>
                    <strong>已配置 API Key</strong>
                    <br />
                    当前使用 {settings.api_key_masked ?? '已保存的 Key'}
                    {settings.api_key_store === 'credential_manager' ? '，保存在 Windows 凭据管理器' : ''}
                  </span>
                </div>
              ) : null}

              <div className="field">
                <label className="field-label" htmlFor="api-key">
                  API Key
                </label>
                <div className="input-affix">
                  <input
                    id="api-key"
                    className="input input-mono"
                    type={showKey ? 'text' : 'password'}
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={settings?.api_key_set ? '如需更换，请粘贴新的 Key' : 'sk-...'}
                    value={apiKey}
                    ref={keyInputRef}
                    onChange={(e) => {
                      setApiKey(e.target.value);
                      setTestResult(null);
                    }}
                  />
                  <button
                    type="button"
                    className="input-affix-btn"
                    aria-label={showKey ? '隐藏 API Key' : '显示 API Key'}
                    title={showKey ? '隐藏' : '显示'}
                    onClick={() => setShowKey((v) => !v)}
                  >
                    {showKey ? <IconEyeOff size={16} /> : <IconEye size={16} />}
                  </button>
                </div>
                <div className="field-hint">
                  Key 保存在 Windows 凭据管理器，界面只会显示掩码，不会明文回显。
                </div>
              </div>

              <div className="row" style={{ marginTop: 'var(--sp-4)', flexWrap: 'wrap' }}>
                <Button
                  icon={<IconKey size={15} />}
                  loading={testing}
                  disabled={!keyReady}
                  onClick={() => void handleTestKey()}
                >
                  测试连接
                </Button>
                <a
                  href={DEEPSEEK_KEYS_URL}
                  onClick={(e) => {
                    e.preventDefault();
                    void handleOpenKeysPage();
                  }}
                >
                  还没有 Key？前往 DeepSeek 获取
                  <IconExternal size={13} style={{ marginLeft: 4, verticalAlign: -2 }} />
                </a>
              </div>

              {testResult ? (
                <div
                  className={testResult.ok ? 'notice notice-ok' : 'err'}
                  style={{ marginTop: 'var(--sp-4)' }}
                  role="status"
                >
                  {testResult.ok ? <IconCheck size={16} /> : <StatusDot status="fail" />}
                  <span>{testResult.text}</span>
                </div>
              ) : null}

              {/* ---------------- 你的打印机（可选） ---------------- */}
              <div className="setting-block">
                <div className="field">
                  <span className="field-label">你的打印机</span>
                  <span className="field-hint">选对机型，AI 才知道该给你设计多厚、多大。</span>
                  <div className="wizard-printers" role="radiogroup" aria-label="你的打印机">
                    {commonPrinters.map(({ id, label, printer }) => {
                      const active = !otherChosen && printers.selectedId === id;
                      return (
                        <button
                          key={id}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          className={`printer-option${active ? ' is-active' : ''}`}
                          disabled={printers.selecting}
                          onClick={() => void handleChoosePrinter(id)}
                        >
                          <span className="printer-option-name">
                            {active ? <IconCheck size={13} /> : <IconNozzle size={13} />}
                            {label}
                          </span>
                          <span className="printer-option-spec">
                            {printer && printer.nozzle_mm > 0
                              ? `${num(printer.nozzle_mm, 2)}mm 喷嘴 · `
                              : '光固化 · '}
                            {printer
                              ? `${num(printer.build_x)}×${num(printer.build_y)}×${num(
                                  printer.build_z,
                                )}mm`
                              : ''}
                            {choosingPrinter === id ? ' · 正在切换…' : ''}
                          </span>
                        </button>
                      );
                    })}
                    <button
                      type="button"
                      role="radio"
                      aria-checked={otherChosen}
                      className={`printer-option${otherChosen ? ' is-active' : ''}`}
                      onClick={() => {
                        setOtherChosen(true);
                        toast.show('可以稍后在「设置 → 打印机」里选择，包括你自己添加的机型。');
                      }}
                    >
                      <span className="printer-option-name">
                        {otherChosen ? <IconCheck size={13} /> : <IconPrinter size={13} />}
                        其他 / 稍后设置
                      </span>
                      <span className="printer-option-spec">不确定就先跳过，不影响使用</span>
                    </button>
                  </div>
                  <span className="field-hint">
                    当前使用：
                    {printers.selected ? printerTitle(printers.selected) : '通用机型 220×220×250'}
                    {printers.selectedMaterial ? ` · ${printers.selectedMaterial}` : ''}
                  </span>
                </div>
              </div>

              <div className="wizard-actions">
                <Button onClick={() => setStep(2)}>上一步</Button>
                <span className="spacer" />
                <Button variant="ghost" loading={finishing} onClick={() => void finish({ skip: true })}>
                  稍后再说
                </Button>
                <Button variant="primary" loading={finishing} onClick={() => void finish()}>
                  完成配置
                </Button>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
