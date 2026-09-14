import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { Settings, SettingsUpdate } from '../api/types';
import { toFriendlyError, type FriendlyError } from './errors';

export interface UseSettingsResult {
  settings: Settings | null;
  loading: boolean;
  saving: boolean;
  error: FriendlyError | null;
  reload: () => Promise<Settings | null>;
  /** 部分更新，成功后用后端返回值覆盖本地状态 */
  save: (patch: SettingsUpdate) => Promise<Settings | null>;
}

/** 用户设置。契约 `GET/PUT /api/settings` —— 永不回传完整 API Key。 */
export function useSettings(options: { auto?: boolean } = {}): UseSettingsResult {
  const { auto = true } = options;
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(auto);
  const [saving, setSaving] = useState(false);
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
      const data = await api.getSettings();
      if (alive.current) {
        setSettings(data);
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

  const save = useCallback(async (patch: SettingsUpdate) => {
    setSaving(true);
    try {
      const data = await api.updateSettings(patch);
      if (alive.current) {
        setSettings(data);
        setError(null);
      }
      return data;
    } catch (err) {
      if (alive.current) setError(toFriendlyError(err));
      return null;
    } finally {
      if (alive.current) setSaving(false);
    }
  }, []);

  useEffect(() => {
    if (!auto) return;
    void reload();
  }, [auto, reload]);

  return { settings, loading, saving, error, reload, save };
}
