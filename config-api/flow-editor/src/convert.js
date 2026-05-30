import { conditionLabel } from './constants'

/**
 * Convert our flow definition JSON → React Flow nodes + edges
 */
export function toReactFlow(definition) {
  if (!definition || !definition.nodes) {
    return { nodes: [], edges: [], entryNodeId: '' }
  }

  const positions = definition._positions || {}
  const entryNodeId = definition.entry_node_id || ''

  const rfNodes = definition.nodes.map((node, i) => {
    // Auto-layout: column of nodes if no stored positions
    const pos = positions[node.id] || {
      x: 300,
      y: 100 + i * 160,
    }
    return {
      id: node.id,
      type: node.type,
      position: pos,
      data: {
        label: node.label || node.type,
        config: node.config || {},
        isEntry: node.id === entryNodeId,
      },
    }
  })

  const rfEdges = (definition.edges || []).map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    label: conditionLabel(edge.condition),
    data: { condition: edge.condition || { type: 'default' } },
    style: { strokeWidth: 2 },
    labelStyle: { fontSize: 11, fill: '#374151', fontWeight: 500 },
    labelBgStyle: { fill: '#f9fafb', fillOpacity: 0.9 },
    labelBgPadding: [4, 4],
    labelBgBorderRadius: 4,
    markerEnd: { type: 'arrowclosed', color: '#6b7280' },
  }))

  return { nodes: rfNodes, edges: rfEdges, entryNodeId }
}

/**
 * Convert React Flow nodes + edges → our flow definition JSON
 */
export function fromReactFlow(rfNodes, rfEdges, entryNodeId) {
  const positions = {}
  rfNodes.forEach((n) => {
    positions[n.id] = { x: Math.round(n.position.x), y: Math.round(n.position.y) }
  })

  const nodes = rfNodes.map((n) => ({
    id: n.id,
    type: n.type,
    label: n.data.label,
    config: n.data.config || {},
  }))

  const edges = rfEdges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    condition: e.data?.condition || { type: 'default' },
  }))

  return {
    entry_node_id: entryNodeId,
    nodes,
    edges,
    _positions: positions,
  }
}

let _idCounter = 1
export function newId(prefix = 'n') {
  return `${prefix}${Date.now()}_${_idCounter++}`
}
