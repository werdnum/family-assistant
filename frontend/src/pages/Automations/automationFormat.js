import { Bot, CalendarClock, ScrollText, Zap } from 'lucide-react';

export const TYPE_METADATA = {
  event: { label: 'Event-Based', shortLabel: 'Event', icon: Zap },
  schedule: { label: 'Schedule-Based', shortLabel: 'Schedule', icon: CalendarClock },
};

export const ACTION_METADATA = {
  wake_llm: { label: 'LLM Callback', icon: Bot },
  script: { label: 'Script Execution', icon: ScrollText },
};

export const getTypeMeta = (type) => TYPE_METADATA[type] ?? TYPE_METADATA.event;

export const getActionMeta = (actionType) => ACTION_METADATA[actionType] ?? ACTION_METADATA.script;

export const automationPath = (automation) => ({
  pathname: `/automations/${automation.type}/${automation.id}`,
  search: `?conversation_id=${encodeURIComponent(automation.conversation_id)}`,
});

export const formatTimestamp = (timestamp) => {
  if (!timestamp) {
    return 'Never';
  }
  return new Date(timestamp).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
};

export const formatSourceId = (sourceId) => {
  if (!sourceId) {
    return '';
  }
  return sourceId.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
};

const WEEKDAYS = {
  MO: 'Monday',
  TU: 'Tuesday',
  WE: 'Wednesday',
  TH: 'Thursday',
  FR: 'Friday',
  SA: 'Saturday',
  SU: 'Sunday',
};

const FREQ_UNITS = {
  MINUTELY: ['minute', 'minutes'],
  HOURLY: ['hour', 'hours'],
  DAILY: ['day', 'days'],
  WEEKLY: ['week', 'weeks'],
  MONTHLY: ['month', 'months'],
  YEARLY: ['year', 'years'],
};

const FREQ_ADVERBS = {
  MINUTELY: 'Every minute',
  HOURLY: 'Hourly',
  DAILY: 'Daily',
  WEEKLY: 'Weekly',
  MONTHLY: 'Monthly',
  YEARLY: 'Yearly',
};

const ordinal = (n) => {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) {
    return `${n}th`;
  }
  return `${n}${{ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] ?? 'th'}`;
};

const formatTime = (hour, minute) => {
  const suffix = hour < 12 ? 'am' : 'pm';
  const displayHour = hour % 12 === 0 ? 12 : hour % 12;
  return minute === 0
    ? `${displayHour}${suffix}`
    : `${displayHour}:${String(minute).padStart(2, '0')}${suffix}`;
};

const joinList = (items) =>
  items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`;

/**
 * Turn a recurrence rule into a short English phrase ("Every Tuesday at 7pm").
 *
 * Only the common parts of RRULE are understood. Anything else (BYSETPOS, COUNT, UNTIL, BYWEEKNO,
 * multiple hours, ...) returns null so callers show the raw rule rather than a misleading summary.
 */
export const describeRecurrenceRule = (rule) => {
  if (!rule) {
    return null;
  }
  const parts = {};
  for (const segment of rule.replace(/^RRULE:/i, '').split(';')) {
    const [key, value] = segment.split('=');
    if (key && value !== undefined) {
      parts[key.toUpperCase()] = value.toUpperCase();
    }
  }

  const supported = new Set(['FREQ', 'INTERVAL', 'BYDAY', 'BYHOUR', 'BYMINUTE', 'BYMONTHDAY']);
  if (!parts.FREQ || !FREQ_UNITS[parts.FREQ] || Object.keys(parts).some((k) => !supported.has(k))) {
    return null;
  }

  const interval = parts.INTERVAL ? Number(parts.INTERVAL) : 1;
  if (!Number.isInteger(interval) || interval < 1) {
    return null;
  }

  let phrase;
  if (parts.BYDAY) {
    const days = parts.BYDAY.split(',');
    if (days.some((day) => !WEEKDAYS[day])) {
      return null;
    }
    const sorted = Object.keys(WEEKDAYS).filter((day) => days.includes(day));
    const isWeekdays = sorted.join(',') === 'MO,TU,WE,TH,FR';
    const isWeekend = sorted.join(',') === 'SA,SU';
    const dayText = isWeekdays
      ? 'weekday'
      : isWeekend
        ? 'Saturday and Sunday'
        : joinList(sorted.map((day) => WEEKDAYS[day]));
    // A daily interval filtered by weekday (e.g. every 2nd day, if it is a weekend) has no
    // faithful weekly phrasing.
    if (parts.FREQ === 'DAILY' && interval === 1) {
      phrase = `Every ${dayText}`;
    } else if (parts.FREQ === 'WEEKLY') {
      phrase = interval === 1 ? `Every ${dayText}` : `Every ${interval} weeks on ${dayText}`;
    } else {
      return null;
    }
  } else if (parts.BYMONTHDAY) {
    const days = parts.BYMONTHDAY.split(',').map(Number);
    if (parts.FREQ !== 'MONTHLY' || days.some((d) => !Number.isInteger(d) || d < 1 || d > 31)) {
      return null;
    }
    const dayText = `the ${joinList(days.map(ordinal))}`;
    phrase = interval === 1 ? `Monthly on ${dayText}` : `Every ${interval} months on ${dayText}`;
  } else if (interval === 1) {
    phrase = FREQ_ADVERBS[parts.FREQ];
  } else {
    phrase = `Every ${interval} ${FREQ_UNITS[parts.FREQ][1]}`;
  }

  if (parts.BYHOUR !== undefined) {
    if (parts.FREQ === 'MINUTELY' || parts.FREQ === 'HOURLY') {
      return null;
    }
    const hours = parts.BYHOUR.split(',').map(Number);
    const minute = parts.BYMINUTE !== undefined ? Number(parts.BYMINUTE) : 0;
    if (
      hours.some((h) => !Number.isInteger(h) || h < 0 || h > 23) ||
      !Number.isInteger(minute) ||
      minute < 0 ||
      minute > 59
    ) {
      return null;
    }
    phrase += ` at ${joinList(hours.map((h) => formatTime(h, minute)))}`;
  } else if (parts.BYMINUTE !== undefined) {
    return null;
  }

  return phrase;
};
