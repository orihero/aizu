import { describe, expect, test } from 'vitest';
import type { InterviewQuestion } from '@/shared/types/domain';
import { answerToText, initialAnswer, isAnswered } from './interviewAnswers';

const SINGLE: InterviewQuestion = {
  id: 'goal',
  type: 'single',
  prompt: 'Goal?',
  options: [
    { value: 'lead', label: 'Find leads' },
    { value: 'traffic', label: 'Drive traffic' },
  ],
  allowCustom: true,
};
const PLATFORMS: InterviewQuestion = { id: 'p', type: 'platforms', prompt: 'Where?', suggested: ['instagram', 'x'] };
const TEXT: InterviewQuestion = { id: 't', type: 'text', prompt: 'Describe your buyer' };

describe('initialAnswer', () => {
  test('pre-selects the suggested platforms', () => {
    expect(initialAnswer(PLATFORMS).values).toEqual(['instagram', 'x']);
  });

  test('starts other question types blank', () => {
    expect(initialAnswer(TEXT)).toEqual({ values: [], customText: '' });
  });
});

describe('isAnswered', () => {
  test('a single question needs a selection or custom text', () => {
    expect(isAnswered(SINGLE, { values: [], customText: '' })).toBe(false);
    expect(isAnswered(SINGLE, { values: ['lead'], customText: '' })).toBe(true);
    expect(isAnswered(SINGLE, { values: [], customText: 'Resellers' })).toBe(true);
  });

  test('a text question needs non-whitespace content', () => {
    expect(isAnswered(TEXT, { values: ['  '], customText: '' })).toBe(false);
    expect(isAnswered(TEXT, { values: ['startups'], customText: '' })).toBe(true);
  });

  test('a platforms question needs at least one platform', () => {
    expect(isAnswered(PLATFORMS, { values: [], customText: '' })).toBe(false);
    expect(isAnswered(PLATFORMS, { values: ['instagram'], customText: '' })).toBe(true);
  });
});

describe('answerToText', () => {
  test('renders the selected option label, appending custom text', () => {
    expect(answerToText(SINGLE, { values: ['lead'], customText: 'and resellers' })).toBe('Find leads, and resellers');
  });

  test('maps platform slugs to display labels', () => {
    expect(answerToText(PLATFORMS, { values: ['instagram', 'x'], customText: '' })).toBe('Instagram, X');
  });

  test('trims free text', () => {
    expect(answerToText(TEXT, { values: ['  startups  '], customText: '' })).toBe('startups');
  });
});
