import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { ProjectSummary } from '../api/types';
import { toFriendlyError, type FriendlyError } from './errors';

export interface UseProjectsResult {
  projects: ProjectSummary[];
  loading: boolean;
  error: FriendlyError | null;
  reload: () => Promise<ProjectSummary[] | null>;
  create: (name: string) => Promise<ProjectSummary | null>;
  rename: (id: string, name: string) => Promise<ProjectSummary | null>;
  remove: (id: string) => Promise<boolean>;
}

/** 项目列表。契约 §4：GET/POST /api/projects、PATCH/DELETE /api/projects/{id} */
export function useProjects(): UseProjectsResult {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<FriendlyError | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listProjects();
      if (alive.current) {
        setProjects(data);
        setError(null);
      }
      return data;
    } catch (err) {
      if (alive.current) setError(toFriendlyError(err));
      return null;
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  const create = useCallback(
    async (name: string) => {
      try {
        const created = await api.createProject({ name });
        if (alive.current) setProjects((prev) => [created, ...prev]);
        return created;
      } catch (err) {
        if (alive.current) setError(toFriendlyError(err));
        return null;
      }
    },
    [],
  );

  const rename = useCallback(async (id: string, name: string) => {
    try {
      const updated = await api.renameProject(id, { name });
      if (alive.current) {
        setProjects((prev) => prev.map((p) => (p.id === id ? { ...p, ...updated } : p)));
      }
      return updated;
    } catch (err) {
      if (alive.current) setError(toFriendlyError(err));
      return null;
    }
  }, []);

  const remove = useCallback(async (id: string) => {
    try {
      await api.deleteProject(id);
      if (alive.current) setProjects((prev) => prev.filter((p) => p.id !== id));
      return true;
    } catch (err) {
      if (alive.current) setError(toFriendlyError(err));
      return false;
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { projects, loading, error, reload, create, rename, remove };
}
