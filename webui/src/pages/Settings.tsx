import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
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
import { useSettings } from '../hooks/useSettings';
import { useTheme, type ThemeChoice } from '../hooks/useTheme';

const MODELS = [
  { value: 'deepseek-chat', label: 'deepseek-chat（通用，速度快）' },
  { value: 'deepseek-reasoner', label: 'deepseek-reasoner（推理更强，稍慢）' },
];

const THEMES: { value: ThemeChoice; label: string }[] = [
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
  { value: 'system', label: '跟随系统' },
];

export default function Settings() {
  const navigate = useNavigate();
  const toast = useToast();
  const { settings, loading, saving, save } = useSettings();
  const { theme, setTheme } = useTheme();
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
  const [testing3d, setTesting3d] = useState(false);
  const [installJobId, setInstallJobId] = useState<string | null>(null);
  const install = useInstallStream(installJobId);

  // 后端设置到达后同步到本地表单
  useEffect(() => {
    if (!settings) return;
    setBaseUrl(settings.base_url);
    setModel(settings.model);
    setTheme(settings.theme, { persist: true });
  }, [settings, setTheme]);

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
      setTestText({
        ok: res.ok,
        text: res.message + (res.latency_ms ? `（${Math.round(res.latency_ms)} 毫秒）` : ''),
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
                  {MODELS.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                  {MODELS.every((m) => m.value !== model) ? (
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
              <span className="field-hint">存放设置、日志和临时文件，可以安全清理缓存部分。</span>
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

          <div className="switch-row">
            <div>
              <div className="field-label">外观</div>
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
