/**
 * Collapsible accordion for prompt sections of a single provenance type.
 *
 * Displays a color-coded header with icon and label, expandable to show
 * the individual sections within that provenance group.
 */

import { useState } from 'react';
import type { PromptSection, SectionProvenance } from '../../types/api';
import { getProvenanceStyle } from '../../config/provenance';

interface PromptSectionGroupProps {
  /** Provenance type for this group. */
  provenance: SectionProvenance;
  /** Sections belonging to this provenance group. */
  sections: PromptSection[];
  /** Whether the group is initially expanded. */
  defaultExpanded?: boolean;
}

export function PromptSectionGroup({
  provenance,
  sections,
  defaultExpanded = false,
}: PromptSectionGroupProps) {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);
  const style = getProvenanceStyle(provenance);

  if (sections.length === 0) {
    return null;
  }

  return (
    <div className={`border rounded-lg overflow-hidden ${style.borderClass}`}>
      {/* Collapsible Header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className={`w-full px-4 py-3 flex items-center justify-between ${style.headerBgClass} hover:opacity-90 transition-opacity`}
      >
        <div className="flex items-center gap-2">
          <span className="text-lg">{style.icon}</span>
          <span className={`font-semibold ${style.textClass}`}>
            {style.label}
          </span>
          <span className={`text-sm ${style.textClass} opacity-70`}>
            ({sections.length} section{sections.length !== 1 ? 's' : ''})
          </span>
        </div>
        <svg
          className={`w-5 h-5 ${style.textClass} transform transition-transform ${
            isExpanded ? 'rotate-180' : ''
          }`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M19 9l-7 7-7-7"
          />
        </svg>
      </button>

      {/* Expandable Content */}
      {isExpanded && (
        <div className={`${style.bgClass} divide-y ${style.borderClass}`}>
          {sections.map((section, idx) => (
            <div key={`${section.tag}-${idx}`} className="p-4">
              {/* Section Tag */}
              <div className="flex items-center gap-2 mb-2">
                <code className={`px-2 py-0.5 rounded text-sm font-mono ${style.headerBgClass} ${style.textClass}`}>
                  &lt;{section.tag}&gt;
                </code>
              </div>
              {/* Section Content */}
              <pre className="text-sm text-gray-700 whitespace-pre-wrap font-mono bg-white/50 p-3 rounded border border-gray-200 overflow-x-auto max-h-96">
                {section.content}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
