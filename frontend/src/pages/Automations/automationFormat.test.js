import { describe, expect, it } from 'vitest';
import { describeRecurrenceRule } from './automationFormat';

describe('describeRecurrenceRule', () => {
  it.each([
    ['FREQ=DAILY;BYHOUR=7;BYMINUTE=0', 'Daily at 7am'],
    ['FREQ=DAILY;BYHOUR=9', 'Daily at 9am'],
    ['FREQ=DAILY;BYHOUR=18;BYMINUTE=30', 'Daily at 6:30pm'],
    ['FREQ=DAILY;BYHOUR=0', 'Daily at 12am'],
    ['FREQ=DAILY;BYHOUR=8,20', 'Daily at 8am and 8pm'],
    ['FREQ=WEEKLY;BYDAY=TU;BYHOUR=19;BYMINUTE=0', 'Every Tuesday at 7pm'],
    ['FREQ=WEEKLY;BYDAY=MO', 'Every Monday'],
    ['FREQ=WEEKLY;BYDAY=FR,MO,WE', 'Every Monday, Wednesday and Friday'],
    ['FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8', 'Every weekday at 8am'],
    ['FREQ=WEEKLY;INTERVAL=2;BYDAY=SA', 'Every 2 weeks on Saturday'],
    ['FREQ=MONTHLY', 'Monthly'],
    ['FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9', 'Monthly on the 1st at 9am'],
    ['FREQ=HOURLY;INTERVAL=4', 'Every 4 hours'],
    ['RRULE:FREQ=DAILY', 'Daily'],
  ])('describes %s', (rule, expected) => {
    expect(describeRecurrenceRule(rule)).toBe(expected);
  });

  it.each([
    [''],
    ['FREQ=MONTHLY;BYDAY=1MO'],
    ['FREQ=DAILY;COUNT=5'],
    ['FREQ=DAILY;BYMINUTE=0,30;BYHOUR=9'],
    ['FREQ=HOURLY;BYHOUR=9'],
    ['nonsense'],
  ])('returns null for rules it cannot summarise faithfully: %s', (rule) => {
    expect(describeRecurrenceRule(rule)).toBeNull();
  });
});
