import { NODE_CONFIGS } from './constants'

export default function NodePanel({ node, entryNodeId, onChange, onSetEntry, onDelete, onClose }) {
  if (!node) return null
  const cfg = NODE_CONFIGS[node.type] || { label: node.type, fields: [] }
  const config = node.data.config || {}

  function updateConfig(key, value) {
    onChange({
      ...node,
      data: { ...node.data, config: { ...config, [key]: value } },
    })
  }

  function updateLabel(value) {
    onChange({ ...node, data: { ...node.data, label: value } })
  }

  return (
    <div className="side-panel">
      <div className="panel-header" style={{ borderLeftColor: cfg.color }}>
        <div>
          <div className="panel-title">{cfg.icon} {cfg.label}</div>
          {node.data.isEntry && <span className="entry-badge">ENTRY NODE</span>}
        </div>
        <button className="panel-close" onClick={onClose}>✕</button>
      </div>

      <div className="panel-body">
        {/* Label */}
        <div className="field-group">
          <label className="field-label">Node Label</label>
          <input
            className="field-input"
            value={node.data.label || ''}
            onChange={(e) => updateLabel(e.target.value)}
            placeholder="Short display name"
          />
        </div>

        {/* Type-specific config fields */}
        {cfg.fields.map((field) => (
          <div className="field-group" key={field.key}>
            <label className="field-label">
              {field.label}
              {field.required && <span className="required">*</span>}
            </label>
            {field.type === 'textarea' ? (
              <textarea
                className="field-input field-textarea"
                value={config[field.key] || ''}
                onChange={(e) => updateConfig(field.key, e.target.value)}
                placeholder={field.placeholder || ''}
                rows={4}
              />
            ) : (
              <input
                className="field-input"
                type={field.type}
                value={config[field.key] ?? ''}
                onChange={(e) => updateConfig(field.key, field.type === 'number' ? Number(e.target.value) : e.target.value)}
                placeholder={field.placeholder || ''}
              />
            )}
          </div>
        ))}

        {/* Node ID (read-only) */}
        <div className="field-group">
          <label className="field-label" style={{ color: '#9ca3af' }}>Node ID</label>
          <input className="field-input" value={node.id} readOnly style={{ color: '#9ca3af', fontSize: 11 }} />
        </div>
      </div>

      <div className="panel-footer">
        {node.id !== entryNodeId && (
          <button className="btn-action btn-primary" onClick={() => onSetEntry(node.id)}>
            Set as Entry
          </button>
        )}
        <button
          className="btn-action btn-danger"
          onClick={() => { if (confirm(`Delete node "${node.data.label}"?`)) onDelete(node.id) }}
        >
          Delete Node
        </button>
      </div>
    </div>
  )
}
