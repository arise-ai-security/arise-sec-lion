/**
 * Card component for displaying a single parsed prompt.
 *
 * Features:
 * - Header with prompt number, type badge, target, and timestamp
 * - Toggle between highlighted view (colored by provenance) and plain raw view
 * - Sections displayed inline with provenance-based text colors
 */

import { useState } from 'react';
import type { ParsedPrompt, PromptSection } from '../../types/api';
import { getProvenanceStyle } from '../../config/provenance';

interface ParsedPromptCardProps {
  /** The parsed prompt to display. */
  prompt: ParsedPrompt;
  /** 1-indexed prompt number. */
  promptNumber: number;
}

/** Badge colors for prompt types. */
const PROMPT_TYPE_COLORS: Record<string, string> = {
  complexity_evaluation: 'bg-blue-100 text-blue-800',
  task_decomposition: 'bg-purple-100 text-purple-800',
  worker_execution: 'bg-green-100 text-green-800',
  researcher_execution: 'bg-cyan-100 text-cyan-800',
};

/** Badge colors for targets. */
const TARGET_COLORS: Record<string, string> = {
  llm: 'bg-gray-100 text-gray-800',
  claude_code: 'bg-orange-100 text-orange-800',
  openhands: 'bg-teal-100 text-teal-800',
  google_adk: 'bg-red-100 text-red-800',
};

/** Text colors for each provenance type (for inline highlighting). */
const PROVENANCE_TEXT_COLORS: Record<string, string> = {
  template: 'text-blue-700',
  parent: 'text-green-700',
  sibling: 'text-yellow-700',
  children: 'text-purple-700',
  shared: 'text-orange-600',
  system: 'text-gray-600',
};

function formatTimestamp(isoString: string): string {
  const date = new Date(isoString);
  return date.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function formatBytes(chars: number): string {
  if (chars < 1000) return `${chars} chars`;
  if (chars < 1000000) return `${(chars / 1000).toFixed(1)}K chars`;
  return `${(chars / 1000000).toFixed(2)}M chars`;
}

/** Render a single section with colored text. */
function ColoredSection({ section }: { section: PromptSection }) {
  const style = getProvenanceStyle(section.provenance);
  const textColor = PROVENANCE_TEXT_COLORS[section.provenance] ?? 'text-gray-700';

  return (
    <span className={textColor} title={`${style.label}: ${section.tag}`}>
      <span className="opacity-60">&lt;{section.tag}&gt;</span>
      {section.content}
      <span className="opacity-60">&lt;/{section.tag}&gt;</span>
    </span>
  );
}

export function ParsedPromptCard({ prompt, promptNumber }: ParsedPromptCardProps) {
  const [showRaw, setShowRaw] = useState(false);

  const promptTypeColor = PROMPT_TYPE_COLORS[prompt.prompt_type] ?? 'bg-gray-100 text-gray-800';
  const targetColor = TARGET_COLORS[prompt.target] ?? 'bg-gray-100 text-gray-800';

  return (
    <div className="bg-white border border-gray-200 rounded-lg shadow-sm">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200 bg-gray-50">
        <div className="flex flex-wrap items-center gap-3">
          {/* Prompt number */}
          <span className="font-bold text-gray-700">
            Prompt #{promptNumber}
          </span>

          {/* Type badge */}
          <span className={`px-2 py-0.5 rounded text-xs font-medium ${promptTypeColor}`}>
            {prompt.prompt_type.replace(/_/g, ' ')}
          </span>

          {/* Target badge */}
          <span className={`px-2 py-0.5 rounded text-xs font-medium ${targetColor}`}>
            {prompt.target}
          </span>

          {/* Size */}
          <span className="text-xs text-gray-500">
            {formatBytes(prompt.raw_length)}
          </span>

          {/* Timestamp */}
          <span className="text-xs text-gray-400">
            {formatTimestamp(prompt.occurred_at)}
          </span>

          {/* View toggle */}
          <div className="ml-auto flex gap-1">
            <button
              onClick={() => setShowRaw(false)}
              className={`px-3 py-1 text-xs rounded ${
                !showRaw
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
              }`}
            >
              Highlighted
            </button>
            <button
              onClick={() => setShowRaw(true)}
              className={`px-3 py-1 text-xs rounded ${
                showRaw
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
              }`}
            >
              Raw
            </button>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="p-4">
        {showRaw ? (
          /* Raw View - plain text */
          <pre className="text-sm text-gray-700 whitespace-pre-wrap font-mono bg-gray-50 p-4 rounded border border-gray-200 overflow-x-auto max-h-[600px] overflow-y-auto">
            {prompt.raw}
          </pre>
        ) : (
          /* Highlighted View - sections with colored text */
          <pre className="text-sm whitespace-pre-wrap font-mono bg-gray-50 p-4 rounded border border-gray-200 overflow-x-auto max-h-[600px] overflow-y-auto">
            {prompt.sections.length > 0 ? (
              prompt.sections.map((section, idx) => (
                <ColoredSection key={`${section.tag}-${idx}`} section={section} />
              ))
            ) : (
              <span className="text-gray-700">{prompt.raw}</span>
            )}
          </pre>
        )}
      </div>
    </div>
  );
}
