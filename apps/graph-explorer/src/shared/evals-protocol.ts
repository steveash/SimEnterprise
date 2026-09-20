// Wire types for the Evals view (docs/EXPLORER_EVALS.md). Mirrors the Python
// side (enterprise_sim.benchmark.schema / .score, enterprise_sim.evals) so the
// renderer and sidecar share one vocabulary with the CLI's JSON output.
import type { AgentEvent } from './agent-events.js'

export const REASONING_TYPES = [
  'direct_relation',
  'transitive',
  'provenance',
  'aggregation',
  'goal_tree'
] as const
export type ReasoningType = (typeof REASONING_TYPES)[number]

/** One KG-QA benchmark question + gold answer (enterprise_sim/benchmark/schema.py QAPair). */
export interface QAPair {
  id: string
  question: string
  qtype: string
  reasoning_type: string
  expected_ids: string[]
  expected_label: string | null
  difficulty: string
  source: 'generated' | 'proposed' | string
  tags: string[]
}

export interface QuestionCounts {
  reasoning_type: Record<string, number>
  qtype: Record<string, number>
  difficulty: Record<string, number>
  source: Record<string, number>
}

/** `evalsList` result: the run's question set, counts, and resolved answer labels. */
export interface EvalsListResult {
  questions: QAPair[]
  counts: QuestionCounts
  /** node id -> display label, resolved from the loaded run's model, for every expected_id seen. */
  expected_labels: Record<string, string>
}

/** Per-question grade — a direct port of enterprise_sim.benchmark.score.ItemScore. */
export interface ItemScore {
  qa_id: string
  reasoning_type: string
  expected: string[]
  predicted: string[]
  exact_match: boolean
  precision: number
  recall: number
  f1: number
}

/** Macro-averaged scores over a group of items (enterprise_sim.benchmark.score.Aggregate). */
export interface Aggregate {
  count: number
  exact_match_rate: number
  macro_precision: number
  macro_recall: number
  macro_f1: number
}

export interface Report {
  items: ItemScore[]
  overall: Aggregate
  by_reasoning_type: Record<string, Aggregate>
}

export type EvalRunner = 'explorer' | 'rag' | 'graph'

export type EvalSelection =
  | { kind: 'ids'; ids: string[] }
  | { kind: 'fraction'; fraction: number; seed: number; stratify: boolean }
  | { kind: 'all' }

/** One line of `<run>/evals/results/<eval-id>/items.jsonl` (EXPLORER_EVALS.md §3). */
export interface EvalItemResult {
  qa_id: string
  question: string
  reasoning_type: string
  expected: string[]
  predicted: string[]
  precision: number
  recall: number
  f1: number
  exact_match: boolean
  turns?: number
  engine?: 'Cypher' | 'SPARQL'
  query?: string
  seconds: number
  cost_usd?: number
  error?: string
}

export type EvalStatus = 'running' | 'done' | 'cancelled' | 'failed'

/** `<run>/evals/results/<eval-id>/eval.json` (EXPLORER_EVALS.md §3). */
export interface EvalExecutionMeta {
  eval_id: string
  runner: EvalRunner
  model: string | null
  selection: EvalSelection
  question_ids: string[]
  started_at: string
  finished_at: string | null
  status: EvalStatus
  usage?: unknown
  cost_usd?: number
  report: Report | null
}

/** One tool call + its result, for a single-question run's live trace (§3/§5). */
export interface TraceEntry {
  id: string
  name: string
  input: unknown
  ok?: boolean
  preview?: string
}

export type EvalStreamEvent =
  | { kind: 'question_started'; id: string }
  | {
      kind: 'question_done'
      id: string
      predicted_ids: string[]
      expected_ids: string[]
      precision: number
      recall: number
      f1: number
      exact: boolean
      engine?: string
      query?: string
      seconds?: number
      cost_usd?: number
      /** Only populated for a single-question run (a batch would make every event heavy for no reason). */
      trace?: TraceEntry[]
    }
  | { kind: 'progress'; done: number; total: number; macro_f1_so_far: number; by_reasoning_type: Record<string, Aggregate> }
  | { kind: 'done'; report: Report }
  | { kind: 'error'; message: string }

/** A question proposed by the propose-chat's `propose_question` tool. */
export interface ProposedQuestion {
  question: string
  reasoning_type: string
  expected_ids: string[]
  expected_label?: string
  difficulty: string
  rationale?: string
  tags: string[]
}

export type ProposeStreamEvent =
  | AgentEvent
  | { kind: 'proposal'; proposal: ProposedQuestion; valid: boolean; reason?: string }
