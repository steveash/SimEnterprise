// Visual vocabulary for node types. Colors chosen to be distinct on the dark canvas.
export const TYPE_COLORS: Record<string, string> = {
  Person: '#4cc9f0',
  Team: '#4895ef',
  Department: '#4361ee',
  Company: '#7209b7',
  Goal: '#f72585',
  Initiative: '#ff9e00',
  Project: '#ffca3a',
  Artifact: '#8ac926',
  CalendarEvent: '#9aa5b1'
}

export const DEFAULT_COLOR = '#adb5bd'

export function typeColor(type: string): string {
  return TYPE_COLORS[type] ?? DEFAULT_COLOR
}

// Edge types that express hierarchy — used by the hierarchical (dagre) layout.
export const HIERARCHY_EDGES = new Set([
  'reports_to',
  'part_of',
  'member_of',
  'has_department',
  'subgoal_of',
  'subinitiative_of',
  'under',
  'owns_initiative'
])

export const MODELS = [
  { id: 'sonnet', label: 'Sonnet 4.6 (fast)' },
  { id: 'opus', label: 'Opus 4.8 (powerful)' },
  { id: 'haiku', label: 'Haiku 4.5 (cheap)' }
]
