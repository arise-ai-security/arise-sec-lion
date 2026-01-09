/**
 * Provenance styling configuration for prompt sections.
 *
 * Organized by the 3-Layer Prompt Architecture:
 *
 * Layer 1: Core Agent Behavior (TEMPLATE)
 *   - Static prompt template content from core/roles/*.j2
 *   - Agent mechanics: BOSS delegates, MANAGER decomposes, WORKER executes
 *
 * Layer 2: Shared Context (PARENT, SIBLING, CHILDREN, SHARED)
 *   - PARENT: Context passed down from parent agent
 *   - SIBLING: Context shared between sibling agents
 *   - CHILDREN: Results/outcomes from child agents
 *   - SHARED: Global shared decisions and artifacts
 *
 * Layer 3: Domain-Specific (SYSTEM)
 *   - SEC-bench: CVE instance data, environment paths, constraints
 *   - User prompt, workspace context, hierarchy limits
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
  /** Which layer this provenance belongs to (1, 2, or 3). */
  layer: 1 | 2 | 3;
  /** Layer name for grouping. */
  layerName: string;
}

export const PROVENANCE_STYLES: Record<SectionProvenance, ProvenanceStyle> = {
  // Layer 1: Core Agent Behavior
  template: {
    label: 'Template',
    icon: '📘',
    bgClass: 'bg-blue-50',
    borderClass: 'border-blue-300',
    textClass: 'text-blue-700',
    headerBgClass: 'bg-blue-100',
    description: 'Agent behavior templates (core/roles/*.j2)',
    layer: 1,
    layerName: 'Core Behavior',
  },
  // Layer 2: Shared Context
  parent: {
    label: 'Parent Context',
    icon: '🌲',
    bgClass: 'bg-green-50',
    borderClass: 'border-green-300',
    textClass: 'text-green-700',
    headerBgClass: 'bg-green-100',
    description: 'Context passed down from parent agent',
    layer: 2,
    layerName: 'Shared Context',
  },
  sibling: {
    label: 'Sibling Context',
    icon: '🔗',
    bgClass: 'bg-yellow-50',
    borderClass: 'border-yellow-400',
    textClass: 'text-yellow-700',
    headerBgClass: 'bg-yellow-100',
    description: 'Context shared between sibling agents',
    layer: 2,
    layerName: 'Shared Context',
  },
  children: {
    label: 'Child Results',
    icon: '📤',
    bgClass: 'bg-purple-50',
    borderClass: 'border-purple-300',
    textClass: 'text-purple-700',
    headerBgClass: 'bg-purple-100',
    description: 'Results and outcomes from child agents',
    layer: 2,
    layerName: 'Shared Context',
  },
  shared: {
    label: 'Global Shared',
    icon: '🌐',
    bgClass: 'bg-orange-50',
    borderClass: 'border-orange-300',
    textClass: 'text-orange-700',
    headerBgClass: 'bg-orange-100',
    description: 'Global shared decisions and artifacts',
    layer: 2,
    layerName: 'Shared Context',
  },
  // Layer 3: Domain-Specific
  system: {
    label: 'Domain',
    icon: '🔒',
    bgClass: 'bg-gray-50',
    borderClass: 'border-gray-300',
    textClass: 'text-gray-600',
    headerBgClass: 'bg-gray-100',
    description: 'SEC-bench CVE data, environment, constraints',
    layer: 3,
    layerName: 'Domain-Specific',
  },
};

/**
 * Order of provenance types for display.
 * Ordered by layer: Layer 1 → Layer 2 → Layer 3
 */
export const PROVENANCE_ORDER: SectionProvenance[] = [
  // Layer 1: Core Behavior
  'template',
  // Layer 2: Shared Context
  'parent',
  'sibling',
  'children',
  'shared',
  // Layer 3: Domain-Specific
  'system',
];

/** Get style for a provenance type with fallback to system. */
export function getProvenanceStyle(provenance: string): ProvenanceStyle {
  return PROVENANCE_STYLES[provenance as SectionProvenance] ?? PROVENANCE_STYLES.system;
}

/** Layer metadata for grouping. */
export const LAYER_INFO: Record<1 | 2 | 3, { name: string; description: string }> = {
  1: {
    name: 'Core Behavior',
    description: 'Agent mechanics from core/roles/*.j2 templates',
  },
  2: {
    name: 'Shared Context',
    description: 'Context passed between agents in hierarchy',
  },
  3: {
    name: 'Domain-Specific',
    description: 'SEC-bench CVE data, environment, constraints',
  },
};

/** Get provenance types grouped by layer. */
export function getProvenanceByLayer(): Map<1 | 2 | 3, SectionProvenance[]> {
  const layers = new Map<1 | 2 | 3, SectionProvenance[]>();

  for (const prov of PROVENANCE_ORDER) {
    const style = PROVENANCE_STYLES[prov];
    const layer = style.layer;
    if (!layers.has(layer)) {
      layers.set(layer, []);
    }
    layers.get(layer)!.push(prov);
  }

  return layers;
}
