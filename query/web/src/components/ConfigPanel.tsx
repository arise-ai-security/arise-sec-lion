/**
 * Panel for displaying system configuration.
 * Shows infrastructure and application settings.
 */

import { useEffect, useState } from 'react';
import * as api from '../api/client';
import type { SystemConfig } from '../types/api';

interface ConfigPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

function ConfigItem({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex justify-between items-center py-1">
      <span className="text-xs text-gray-500 dark:text-gray-400">{label}</span>
      <span className="text-xs font-mono text-gray-700 dark:text-gray-300">{value}</span>
    </div>
  );
}

function ConfigSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">
        {title}
      </h3>
      <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded-lg space-y-1">
        {children}
      </div>
    </div>
  );
}

export function ConfigPanel({ isOpen, onClose }: ConfigPanelProps) {
  const [config, setConfig] = useState<SystemConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen) {
      loadConfig();
    }
  }, [isOpen]);

  async function loadConfig() {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getSystemConfig();
      setConfig(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load config');
    } finally {
      setLoading(false);
    }
  }

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-md mx-4 max-h-[80vh] overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b dark:border-gray-700">
          <h2 className="font-semibold text-gray-800 dark:text-white">
            System Configuration
          </h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-gray-100 dark:hover:bg-gray-800 rounded"
          >
            <svg className="w-5 h-5 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="p-4 overflow-y-auto max-h-[calc(80vh-4rem)]">
          {loading && (
            <div className="text-center py-8 text-gray-500">
              <div className="animate-pulse">Loading configuration...</div>
            </div>
          )}

          {error && (
            <div className="bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 p-3 rounded-lg text-sm">
              {error}
            </div>
          )}

          {config && !loading && (
            <>
              <ConfigSection title="Infrastructure">
                <ConfigItem label="BOSS Model" value={config.infrastructure.llm_model_boss} />
                <ConfigItem label="Worker Tool" value={config.infrastructure.worker_tool_type} />
                <ConfigItem label="Worker Model" value={config.infrastructure.worker_tool_model} />
                <ConfigItem label="Worker Timeout" value={`${config.infrastructure.worker_tool_timeout}s`} />
              </ConfigSection>

              <ConfigSection title="Application">
                <ConfigItem label="Max Retries" value={config.application.max_retries} />
                <ConfigItem label="Retry Delay" value={`${config.application.retry_delay}s`} />
                <ConfigItem label="Poll Interval" value={`${config.application.poll_interval}s`} />
                <ConfigItem label="LLM Timeout" value={`${config.application.llm_timeout}s`} />
                <ConfigItem label="Worker Timeout" value={`${config.application.worker_timeout}s`} />
                <ConfigItem label="Complexity Threshold" value={config.application.default_task_complexity_threshold} />
              </ConfigSection>

              <p className="text-xs text-gray-400 dark:text-gray-500 text-center mt-4">
                Configuration is read-only. Edit config/default.yaml to change.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
