import { useEffect, useMemo, useRef, useState } from 'react';
import type { ProjectSummary } from '../api/types';
import { thumbnailUrl } from '../api/client';
import { Button } from './Button';
import { IconCube, IconFolder, IconMore, IconPencil, IconPlus, IconSearch, IconTrash } from './Icons';
import { useEscapeToClose } from './Accordion';

interface ProjectSidebarProps {
  projects: ProjectSummary[];
  loading: boolean;
  activeId: string | null;
  search: string;
  onSearch: (value: string) => void;
  onOpen: (project: ProjectSummary) => void;
  onCreate: () => void;
  onRename: (project: ProjectSummary) => void;
  onDelete: (project: ProjectSummary) => void;
  onOpenFolder: (project: ProjectSummary) => void;
  creating?: boolean;
}

/** 相对时间：今天 / 昨天 / N 天前，避免出现英文时间格式 */
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const diffMs = Date.now() - then;
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days === 1) return '昨天';
  if (days < 30) return `${days} 天前`;
  const d = new Date(then);
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
}

const STATUS_LABEL: Record<ProjectSummary['last_status'], string> = {
  ok: '模型正常',
  error: '上次出错',
  unknown: '尚未生成',
};

export function ProjectSidebar({
  projects,
  loading,
  activeId,
  search,
  onSearch,
  onOpen,
  onCreate,
  onRename,
  onDelete,
  onOpenFolder,
  creating,
}: ProjectSidebarProps) {
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEscapeToClose(menuFor !== null, () => setMenuFor(null));

  useEffect(() => {
    if (!menuFor) return;
    const onDocClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuFor(null);
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [menuFor]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return projects;
    return projects.filter((p) => p.name.toLowerCase().includes(q));
  }, [projects, search]);

  return (
    <aside className="side" aria-label="项目列表">
      <div className="side-head">
        <div className="row" style={{ gap: 6, position: 'relative' }}>
          <IconSearch
            size={15}
            style={{
              position: 'absolute',
              left: 10,
              color: 'var(--text-3)',
              pointerEvents: 'none',
            }}
          />
          <input
            className="input side-search"
            style={{ paddingLeft: 31 }}
            placeholder="搜索项目"
            aria-label="搜索项目"
            value={search}
            onChange={(e) => onSearch(e.target.value)}
          />
        </div>
        <Button
          icon={<IconPlus size={15} />}
          block
          loading={creating}
          onClick={onCreate}
          className="side-new"
        >
          <span className="btn-label">新建项目</span>
        </Button>
      </div>

      <div className="side-list">
        {loading && projects.length === 0 ? (
          <div style={{ padding: '4px 8px' }}>
            {[0, 1, 2, 3].map((i) => (
              <div className="skeleton-row" key={i} style={{ borderBottom: 'none' }}>
                <div className="skeleton" style={{ width: 44, height: 44, borderRadius: 9 }} />
                <div style={{ flex: 1 }}>
                  <div className="skeleton" style={{ height: 11, width: '72%', marginBottom: 7 }} />
                  <div className="skeleton" style={{ height: 9, width: '46%' }} />
                </div>
              </div>
            ))}
          </div>
        ) : null}

        {!loading && filtered.length === 0 ? (
          <div className="empty" style={{ padding: 'var(--sp-5) var(--sp-3)' }}>
            <IconCube size={22} />
            <div className="small">
              {projects.length === 0 ? '还没有项目' : '没有匹配的项目'}
            </div>
            {projects.length === 0 ? (
              <div className="xs muted-3">在右侧描述你想要的模型即可开始</div>
            ) : null}
          </div>
        ) : null}

        {filtered.map((project) => (
          <div
            key={project.id}
            className={`proj${project.id === activeId ? ' is-active' : ''}`}
            role="button"
            tabIndex={0}
            aria-label={`打开项目 ${project.name}`}
            onClick={() => onOpen(project)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onOpen(project);
              }
            }}
            style={{ position: 'relative' }}
          >
            {project.thumbnail_url ? (
              <img
                className="proj-thumb"
                src={thumbnailUrl(project.id)}
                alt=""
                loading="lazy"
                onError={(e) => {
                  e.currentTarget.style.display = 'none';
                }}
              />
            ) : (
              <span className="proj-thumb-fallback">
                <IconCube size={18} />
              </span>
            )}

            <div className="proj-main">
              <div className="proj-name" title={project.name}>
                {project.name}
              </div>
              <div className="proj-meta">
                <span
                  className="dot"
                  style={{
                    background:
                      project.last_status === 'ok'
                        ? 'var(--ok)'
                        : project.last_status === 'error'
                          ? 'var(--danger)'
                          : 'var(--text-3)',
                  }}
                  aria-hidden="true"
                />
                <span>{STATUS_LABEL[project.last_status]}</span>
                <span>·</span>
                <span>{relativeTime(project.updated_at)}</span>
              </div>
            </div>

            <button
              type="button"
              className="proj-more"
              aria-label={`${project.name} 的更多操作`}
              aria-expanded={menuFor === project.id}
              aria-haspopup="menu"
              onClick={(e) => {
                e.stopPropagation();
                setMenuFor((cur) => (cur === project.id ? null : project.id));
              }}
            >
              <IconMore size={16} />
            </button>

            {menuFor === project.id ? (
              <div
                className="menu"
                role="menu"
                ref={menuRef}
                style={{ top: 42, right: 6 }}
                onClick={(e) => e.stopPropagation()}
              >
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenuFor(null);
                    onRename(project);
                  }}
                >
                  <IconPencil size={15} />
                  重命名
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenuFor(null);
                    onOpenFolder(project);
                  }}
                >
                  <IconFolder size={15} />
                  打开文件夹
                </button>
                <div className="menu-sep" />
                <button
                  type="button"
                  role="menuitem"
                  className="is-danger"
                  onClick={() => {
                    setMenuFor(null);
                    onDelete(project);
                  }}
                >
                  <IconTrash size={15} />
                  删除
                </button>
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </aside>
  );
}
