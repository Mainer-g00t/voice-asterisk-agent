import { Handle, Position } from 'reactflow'
import { NODE_CONFIGS } from './constants'

function truncate(str, n = 55) {
  if (!str) return ''
  return str.length > n ? str.slice(0, n) + '…' : str
}

function NodePreview({ type, config }) {
  if (!config) return null
  switch (type) {
    case 'conversation':
      return config.system_prompt
        ? <div className="node-preview">{truncate(config.system_prompt)}</div>
        : null
    case 'say':
      return config.message
        ? <div className="node-preview">{truncate(config.message)}</div>
        : null
    case 'gather_dtmf':
      return <div className="node-preview">timeout: {config.dtmf_timeout ?? 10}s</div>
    case 'transfer':
      return config.destination
        ? <div className="node-preview">→ {config.destination}</div>
        : null
    case 'webhook': {
      let display = config.url || ''
      try { display = new URL(config.url).hostname } catch {}
      return display ? <div className="node-preview">{display}</div> : null
    }
    case 'set_variable':
      return config.variable_name
        ? <div className="node-preview">{config.variable_name} = {config.value}</div>
        : null
    case 'condition':
      return config.variable_name
        ? <div className="node-preview">if {config.variable_name}</div>
        : null
    default:
      return null
  }
}

function FlowNode({ type, data, selected }) {
  const cfg = NODE_CONFIGS[type] || { color: '#6b7280', icon: '?', label: type }
  return (
    <div
      className={`flow-node ${selected ? 'selected' : ''}`}
      style={{ '--node-color': cfg.color }}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="node-handle"
      />
      <div className="node-header">
        <span className="node-icon">{cfg.icon}</span>
        <span className="node-type-label">{cfg.label}</span>
        {data.isEntry && <span className="entry-badge">ENTRY</span>}
      </div>
      <div className="node-name">{data.label}</div>
      <NodePreview type={type} config={data.config} />
      <Handle
        type="source"
        position={Position.Bottom}
        className="node-handle"
      />
    </div>
  )
}

export const ConversationNode  = (p) => <FlowNode {...p} type="conversation" />
export const SayNode           = (p) => <FlowNode {...p} type="say" />
export const GatherDtmfNode    = (p) => <FlowNode {...p} type="gather_dtmf" />
export const TransferNode      = (p) => <FlowNode {...p} type="transfer" />
export const WebhookNode       = (p) => <FlowNode {...p} type="webhook" />
export const SetVariableNode   = (p) => <FlowNode {...p} type="set_variable" />
export const ConditionNode     = (p) => <FlowNode {...p} type="condition" />
export const EndNode           = (p) => <FlowNode {...p} type="end" />

export const nodeTypes = {
  conversation: ConversationNode,
  say: SayNode,
  gather_dtmf: GatherDtmfNode,
  transfer: TransferNode,
  webhook: WebhookNode,
  set_variable: SetVariableNode,
  condition: ConditionNode,
  end: EndNode,
}
