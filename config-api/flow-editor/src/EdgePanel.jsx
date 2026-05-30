import { CONDITION_TYPES, CONDITION_FIELDS } from './constants'

export default function EdgePanel({ edge, onChange, onDelete, onClose }) {
  if (!edge) return null
  const condition = edge.data?.condition || { type: 'default' }

  function updateCondition(updates) {
    const newCondition = { ...condition, ...updates }
    onChange({
      ...edge,
      data: { condition: newCondition },
    })
  }

  function changeType(newType) {
    updateCondition({ type: newType })
  }

  function updateField(key, value) {
    // For keyword_matched, convert comma-separated string to array
    if (condition.type === 'keyword_matched' && key === 'words') {
      updateCondition({ [key]: value.split(',').map((w) => w.trim()).filter(Boolean) })
    } else {
      updateCondition({ [key]: value })
    }
  }

  const extraFields = CONDITION_FIELDS[condition.type] || []

  // For display, keyword words as comma-string
  function fieldDisplayValue(field) {
    if (condition.type === 'keyword_matched' && field.key === 'words') {
      return Array.isArray(condition.words) ? condition.words.join(', ') : (condition.words || '')
    }
    return condition[field.key] ?? ''
  }

  return (
    <div className="side-panel">
      <div className="panel-header" style={{ borderLeftColor: '#6b7280' }}>
        <div className="panel-title">→ Edge Condition</div>
        <button type="button" className="panel-close" onClick={onClose}>✕</button>
      </div>

      <div className="panel-body">
        <div className="field-group">
          <label className="field-label">Condition Type</label>
          <select
            className="field-input"
            value={condition.type || 'default'}
            onChange={(e) => changeType(e.target.value)}
          >
            {CONDITION_TYPES.map((ct) => (
              <option key={ct.value} value={ct.value}>{ct.label}</option>
            ))}
          </select>
        </div>

        {extraFields.map((field) => (
          <div className="field-group" key={field.key}>
            <label className="field-label">{field.label}</label>
            <input
              className="field-input"
              type={field.type}
              value={fieldDisplayValue(field)}
              onChange={(e) => updateField(field.key, field.type === 'number' ? Number(e.target.value) : e.target.value)}
              placeholder={field.placeholder || ''}
            />
          </div>
        ))}

        <div className="field-group">
          <label className="field-label" style={{ color: '#9ca3af' }}>Edge ID</label>
          <input className="field-input" value={edge.id} readOnly style={{ color: '#9ca3af', fontSize: 11 }} />
        </div>
      </div>

      <div className="panel-footer">
        <button type="button"
          className="btn-action btn-danger"
          onClick={() => { if (confirm('Delete this edge?')) onDelete(edge.id) }}
        >
          Delete Edge
        </button>
      </div>
    </div>
  )
}
