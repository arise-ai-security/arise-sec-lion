/**
 * Provenance styling configuration for prompt sections.
 *
 * Each provenance type has a distinct visual style:
 * - TEMPLATE: Static prompt template content (blue)
 * - PARENT: Context passed from parent agent (green)
 * - SIBLING: Context shared between sibling agents (yellow)
 * - CHILDREN: Results/outcomes from child agents (purple)
 * - SHARED: Global shared context (decisions, artifacts) (orange)
 * - SYSTEM: System-injected context (domain context, workspace) (gray)
 */

import type { SectionProvenance } from '../types/api';

export interface ProvenanceStyle {
  /** Display label for this provenance type. */
  label: string;
  /** Icon/emoji for visual identification. */
  icon: string;
  /** Tailwind CSS class for background color. */
  bgClass: string;
  /** Tailwind CSS class for border color. */
  borderClass: string;
  /** Tailwind CSS class for text color. */
  textClass: string;
  /** Tailwind CSS class for header background. */
  headerBgClass: string;
  /** Description of what this provenance type represents. */
  description: string;
}

export const PROVENANCE_STYLES: Record<SectionProvenance, ProvenanceStyle> = {
  template: {
    label: 'Template',
    icon: '📘',
    bgClass: 'bg-blue-50',
    borderClass: 'border-blue-300',
    textClass: 'text-blue-700',
    headerBgClass: 'bg-blue-100',
    description: 'Static template content from prompt files',
  },
  parent: {
    label: 'Parent Context',
    icon: '🌲',
    bgClass: 'bg-green-50',
    borderClass: 'border-green-300',
    textClass: 'text-green-700',
    headerBgClass: 'bg-green-100',
    description: 'Context passed down from parent agent',
  },
  sibling: {
    label: 'Sibling Context',
    icon: '🔗',
    bgClass: 'bg-yellow-50',
    borderClass: 'border-yellow-400',
    textClass: 'text-yellow-700',
    headerBgClass: 'bg-yellow-100',
    description: 'Context shared between sibling agents',
  },
  children: {
    label: 'Child Results',
    icon: '📤',
    bgClass: 'bg-purple-50',
    borderClass: 'border-purple-300',
    textClass: 'text-purple-700',
    headerBgClass: 'bg-purple-100',
    description: 'Results and outcomes from child agents',
  },
  shared: {
    label: 'Global Shared',
    icon: '🌐',
    bgClass: 'bg-orange-50',
    borderClass: 'border-orange-300',
    textClass: 'text-orange-700',
    headerBgClass: 'bg-orange-100',
    description: 'Global shared decisions and artifacts',
  },
  system: {
    label: 'System',
    icon: '⚙️',
    bgClass: 'bg-gray-50',
    borderClass: 'border-gray-300',
    textClass: 'text-gray-600',
    headerBgClass: 'bg-gray-100',
    description: 'System-injected context (domain context, workspace, user prompt)',
  },
};

/** Order of provenance types for display. */
export const PROVENANCE_ORDER: SectionProvenance[] = [
  'system',
  'template',
  'parent',
  'sibling',
  'children',
  'shared',
];

/** Get style for a provenance type with fallback to system. */
export function getProvenanceStyle(provenance: string): ProvenanceStyle {
  return PROVENANCE_STYLES[provenance as SectionProvenance] ?? PROVENANCE_STYLES.system;
}
