import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { VersionInfo } from '../api/types';
import { Accordion } from '../components/Accordion';
import { Card } from '../components/Card';
import { IconExternal, IconLogo } from '../components/Icons';
import { toFriendlyError } from '../hooks/errors';

interface FaqItem {
  q: string;
  a: React.ReactNode;
}

const FAQ: FaqItem[] = [
  {
    q: 'OpenSCAD 安装失败怎么办',
    a: (
      <>
        <p>
          自动安装会优先下载免安装版本到你的用户目录，因此大多数情况下不需要管理员权限。如果失败：
        </p>
        <ol>
          <li>点击失败提示里的「重试」，Windows 弹出授权提示时选择「是」。</li>
          <li>
            如果电脑由公司统一管理，可能禁止安装软件：请到 OpenSCAD 官方网站下载安装包，手动安装后在
            「设置 → 3D 引擎」里点击「浏览…」选中 openscad.exe。
          </li>
          <li>仍然不行时，在「系统诊断」里对「3D 引擎」执行「自动修复」，会重新检测并写入配置。</li>
        </ol>
        <p>OpenSCAD 是独立的免费开源软件，不是本程序的一部分。</p>
      </>
    ),
  },
  {
    q: 'API Key 无效',
    a: (
      <>
        <p>提示「API Key 无效或额度不足」时，请检查三件事：</p>
        <ol>
          <li>Key 是否复制完整（以 sk- 开头，前后没有多余空格）。</li>
          <li>到 DeepSeek 平台确认 Key 仍然有效、没有被删除。</li>
          <li>确认账户余额或免费额度还没有用完。</li>
        </ol>
        <p>
          修改路径：设置 → AI 服务 → 更换。保存后界面只显示掩码，不会明文回显，这是有意设计的。
        </p>
      </>
    ),
  },
  {
    q: '模型编译失败',
    a: (
      <>
        <p>常见原因是尺寸或壁厚在几何上无法成立，例如孔径大于外形、壁厚为 0。</p>
        <ol>
          <li>在对话里点「自动修复」，让 AI 根据报错重新生成一版。</li>
          <li>把要求写得更具体，例如「外径 80mm、壁厚 3mm 的花盆」。</li>
          <li>展开结果卡片里的「技术详情」，可以看到具体的失败原因。</li>
        </ol>
      </>
    ),
  },
  {
    q: 'STL 下载后打不开',
    a: (
      <>
        <p>
          STL 是三维模型文件，不能用看图软件打开。请用切片软件打开，例如 Bambu Studio、PrusaSlicer、Cura
          或 Windows 自带的「3D 查看器」。
        </p>
        <p>如果切片软件提示模型有问题，先看结果卡片里的「打印评分」和问题清单，按建议调整尺寸再重新生成。</p>
      </>
    ),
  },
  {
    q: '数据保存在哪里',
    a: (
      <>
        <p>
          全部保存在你的 Windows 用户目录：项目文件在「文档 → AI-3D-Forge → Projects」，配置与缓存在
          「AppData → Roaming → AI-3D-Forge」。
        </p>
        <p>
          这些位置不在程序安装目录里，卸载或升级都不会删除。API Key 额外存放在 Windows
          凭据管理器，只以掩码形式显示。具体路径可以在「设置 → 文件位置」里一键打开。
        </p>
      </>
    ),
  },
  {
    q: '生成的模型可以直接打印吗',
    a: (
      <>
        <p>
          可以。每次生成后都会做一次可打印性检查，给出 0-100 的评分和具体建议，例如壁厚是否足够、悬空部分是否需要支撑。
        </p>
        <p>导出的是毫米单位的 STL，直接拖进切片软件即可；尺寸可以在「参数微调」里改动，不需要重新描述。</p>
      </>
    ),
  },
];

const LICENSES = [
  {
    name: 'Three.js',
    license: 'MIT',
    note: '用于界面里的三维预览。',
  },
  {
    name: 'FastAPI',
    license: 'MIT',
    note: '本程序本地服务的接口框架。',
  },
  {
    name: 'React / Vite / TypeScript',
    license: 'MIT',
    note: '界面与构建工具链。',
  },
  {
    name: 'OpenSCAD',
    license: 'GPLv2',
    note: '独立的开源三维建模软件，由本程序调用，不随本程序分发。',
  },
];

export default function About() {
  const [version, setVersion] = useState<VersionInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void api
      .envVersion()
      .then((v) => {
        if (alive) setVersion(v);
      })
      .catch((err) => {
        if (alive) setError(toFriendlyError(err).message);
      });
    return () => {
      alive = false;
    };
  }, []);

  const rows: { label: string; value: string }[] = version
    ? [
        { label: '软件版本', value: version.app_full || `AI 3D Forge ${version.app}` },
        { label: '构建号', value: version.build },
        {
          label: '运行环境',
          value: version.python_bundled
            ? `${version.python}（已内置，无需单独安装）`
            : version.python,
        },
        { label: '3D 引擎', value: version.openscad ? `OpenSCAD ${version.openscad}` : '未安装' },
        { label: '操作系统', value: version.os },
      ]
    : [];

  return (
    <div className="page">
      <div className="about-hero">
        <span className="hero-mark" style={{ marginBottom: 0 }}>
          <IconLogo size={26} />
        </span>
        <div>
          <h1 style={{ marginBottom: 4 }}>AI 3D Forge</h1>
          <p className="muted">用一句话描述，得到可以直接打印的 3D 模型。</p>
        </div>
      </div>

      <div className="stack">
        <Card>
          <div className="card-head">
            <div>
              <h2>版本信息</h2>
              <div className="card-hint">排查问题时可以把这些信息提供给技术支持。</div>
            </div>
          </div>

          {error ? <div className="muted small">{error}</div> : null}

          {!version && !error ? (
            <div>
              {[0, 1, 2, 3].map((i) => (
                <div className="skeleton" key={i} style={{ height: 12, marginBottom: 10, width: `${70 - i * 8}%` }} />
              ))}
            </div>
          ) : null}

          {version ? (
            <dl className="dl" style={{ fontSize: 'var(--fs-sm)' }}>
              {rows.map((row) => (
                <div key={row.label} style={{ display: 'contents' }}>
                  <dt style={{ fontFamily: 'var(--font)' }}>{row.label}</dt>
                  <dd style={{ fontFamily: 'var(--font)' }}>{row.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}
        </Card>

        <Card>
          <div className="card-head">
            <div>
              <h2>常见问题</h2>
              <div className="card-hint">遇到问题先看这里，多数情况可以自己解决。</div>
            </div>
          </div>
          {FAQ.map((item) => (
            <Accordion title={item.q} key={item.q}>
              {item.a}
            </Accordion>
          ))}
        </Card>

        <Card>
          <div className="card-head">
            <div>
              <h2>开源许可</h2>
              <div className="card-hint">本程序使用了以下开源项目，在此致谢。</div>
            </div>
          </div>
          {LICENSES.map((item) => (
            <div className="license" key={item.name}>
              <div>
                <div style={{ fontWeight: 550 }}>{item.name}</div>
                <div className="muted small">{item.note}</div>
              </div>
              <span className="tag">{item.license}</span>
            </div>
          ))}
          <p className="field-hint" style={{ marginTop: 'var(--sp-4)' }}>
            OpenSCAD 是独立的开源程序，需要从
            <a
              href="https://openscad.org"
              onClick={(e) => {
                e.preventDefault();
                void api
                  .openExternal('https://openscad.org')
                  .catch(() => window.open('https://openscad.org', '_blank', 'noopener'));
              }}
            >
              {' '}
              官方网站{' '}
              <IconExternal size={12} style={{ verticalAlign: -2 }} />
            </a>
            下载，不由本程序分发。它的源代码遵循 GPLv2 许可。
          </p>
        </Card>

        <Card>
          <div className="card-head">
            <div>
              <h2>隐私</h2>
            </div>
          </div>
          <p className="muted small">
            本程序只在本机运行。你的描述会发送给所配置的 AI 服务用于生成模型，除此之外不会上传任何数据；项目文件、API
            Key 都保存在这台电脑上。
          </p>
        </Card>
      </div>
    </div>
  );
}
