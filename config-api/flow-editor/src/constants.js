export const NODE_CONFIGS = {
  conversation: {
    color: '#2563eb',
    icon: '💬',
    label: 'Conversation',
    fields: [
      { key: 'system_prompt', label: 'System Prompt', type: 'textarea', required: true },
      { key: 'greeting', label: 'Greeting', type: 'text', placeholder: 'Optional opening message' },
      { key: 'silence_timeout_seconds', label: 'Silence Timeout (s)', type: 'number', placeholder: '10' },
    ],
  },
  say: {
    color: '#d97706',
    icon: '🔊',
    label: 'Say',
    fields: [
      { key: 'message', label: 'Message', type: 'textarea', required: true },
    ],
  },
  gather_dtmf: {
    color: '#ea580c',
    icon: '🔢',
    label: 'Gather DTMF',
    fields: [
      { key: 'prompt', label: 'Prompt (optional TTS before waiting)', type: 'text' },
      { key: 'dtmf_timeout', label: 'Timeout (s)', type: 'number', placeholder: '10' },
    ],
  },
  transfer: {
    color: '#dc2626',
    icon: '↗',
    label: 'Transfer',
    fields: [
      { key: 'destination', label: 'Destination', type: 'text', placeholder: 'operator or +15551234567', required: true },
      { key: 'dialplan_context', label: 'Dialplan Context', type: 'text', placeholder: 'default' },
    ],
  },
  webhook: {
    color: '#7c3aed',
    icon: '🔗',
    label: 'Webhook',
    fields: [
      { key: 'url', label: 'URL', type: 'text', placeholder: 'http://tools-server:8100/endpoint', required: true },
      { key: 'timeout', label: 'Timeout (s)', type: 'number', placeholder: '10' },
    ],
  },
  set_variable: {
    color: '#059669',
    icon: '📝',
    label: 'Set Variable',
    fields: [
      { key: 'variable_name', label: 'Variable Name', type: 'text', required: true },
      { key: 'value', label: 'Value', type: 'text', required: true },
    ],
  },
  condition: {
    color: '#b45309',
    icon: '◆',
    label: 'Condition',
    fields: [
      { key: 'variable_name', label: 'Variable Name', type: 'text', required: true },
    ],
  },
  end: {
    color: '#374151',
    icon: '⏹',
    label: 'End',
    fields: [],
  },
}

export const CONDITION_TYPES = [
  { value: 'keyword_matched', label: 'Keyword matched' },
  { value: 'turn_count_gte', label: 'Turn count ≥ N' },
  { value: 'dtmf_digit', label: 'DTMF digit pressed' },
  { value: 'tool_result', label: 'Tool result field' },
  { value: 'variable_equals', label: 'Variable equals' },
  { value: 'silence_timeout', label: 'Silence timeout' },
  { value: 'webhook_field', label: 'Webhook field equals' },
  { value: 'default', label: 'Default (always fires)' },
]

export const CONDITION_FIELDS = {
  keyword_matched: [
    { key: 'words', label: 'Keywords (comma-separated)', type: 'text', placeholder: 'bye, goodbye, hang up' },
  ],
  turn_count_gte: [
    { key: 'n', label: 'Minimum turn count', type: 'number', placeholder: '5' },
  ],
  dtmf_digit: [
    { key: 'digit', label: 'Digit (0–9, *, #)', type: 'text', placeholder: '1' },
  ],
  tool_result: [
    { key: 'tool', label: 'Tool name', type: 'text' },
    { key: 'field', label: 'Response field', type: 'text' },
    { key: 'value', label: 'Expected value', type: 'text' },
  ],
  variable_equals: [
    { key: 'var', label: 'Variable name', type: 'text' },
    { key: 'value', label: 'Expected value', type: 'text' },
  ],
  silence_timeout: [],
  webhook_field: [
    { key: 'field', label: 'JSON field', type: 'text' },
    { key: 'value', label: 'Expected value', type: 'text' },
  ],
  default: [],
}

export function conditionLabel(condition) {
  if (!condition) return 'default'
  switch (condition.type) {
    case 'keyword_matched': {
      const words = Array.isArray(condition.words) ? condition.words.join(', ') : (condition.words || '')
      return `keyword: ${words.slice(0, 30)}${words.length > 30 ? '…' : ''}`
    }
    case 'turn_count_gte': return `turns ≥ ${condition.n ?? '?'}`
    case 'dtmf_digit': return `digit: ${condition.digit ?? '?'}`
    case 'tool_result': return `tool: ${condition.tool ?? '?'}.${condition.field ?? '?'}=${condition.value ?? '?'}`
    case 'variable_equals': return `${condition.var ?? '?'}=${condition.value ?? '?'}`
    case 'silence_timeout': return 'silence'
    case 'webhook_field': return `webhook: ${condition.field ?? '?'}=${condition.value ?? '?'}`
    case 'default': return 'default'
    default: return condition.type || 'unknown'
  }
}
