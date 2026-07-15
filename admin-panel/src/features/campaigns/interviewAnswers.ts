import { platformLabel } from '@/shared/lib/platformLabel';
import type { InterviewQuestion } from '@/shared/types/domain';

/**
 * The local UI state for one in-progress answer. `values` holds selected option
 * values (single = one, multi/platforms = many, text = the typed string in [0]);
 * `customText` holds the optional "Other" free-text for single/multi questions.
 */
export interface AnswerState {
  readonly values: readonly string[];
  readonly customText: string;
}

export const EMPTY_ANSWER: AnswerState = { values: [], customText: '' };

/** Seed a question's answer: platforms pre-select the AI's suggestions. */
export function initialAnswer(question: InterviewQuestion): AnswerState {
  if (question.type === 'platforms') {
    return { values: [...(question.suggested ?? [])], customText: '' };
  }
  return EMPTY_ANSWER;
}

/** The human label for an option value (falls back to the value itself). */
function optionLabel(question: InterviewQuestion, value: string): string {
  const match = question.options?.find((o) => o.value === value);
  return match?.label ?? value;
}

/** Whether the question has a usable answer (enables "Continue"). */
export function isAnswered(question: InterviewQuestion, answer: AnswerState): boolean {
  switch (question.type) {
    case 'text':
      return (answer.values[0] ?? '').trim().length > 0;
    case 'platforms':
      return answer.values.length > 0;
    case 'single':
    case 'multi':
      return answer.values.length > 0 || answer.customText.trim().length > 0;
    default:
      return false;
  }
}

/** Render the answer to the readable string sent to the model as the transcript. */
export function answerToText(question: InterviewQuestion, answer: AnswerState): string {
  const custom = answer.customText.trim();
  switch (question.type) {
    case 'text':
      return (answer.values[0] ?? '').trim();
    case 'platforms':
      return answer.values.map((v) => platformLabel(v)).join(', ');
    case 'single':
    case 'multi': {
      const labels = answer.values.map((v) => optionLabel(question, v));
      if (custom) labels.push(custom);
      return labels.join(', ');
    }
    default:
      return '';
  }
}
