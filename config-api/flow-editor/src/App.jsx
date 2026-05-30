import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  MarkerType,
} from 'reactflow'
import 'reactflow/dist/style.css'
import './App.css'

import { nodeTypes } from './nodeTypes'
import NodePanel from './NodePanel'
import EdgePanel from './EdgePanel'
import { toReactFlow, fromReactFlow, newId } from './convert'
import { NODE_CONFIGS, conditionLabel } from './constants'

const DEFAULT_FLOW = {
  entry_node_id: 'n1',
  nodes: [
    { id: 'n1', type: 'conversation', label: 'Main', config: { system_prompt: '', greeting: '' } },
  ],
  edges: [],
}

export default function App() {
  // Read initial definition from DOM data attribute
  const mountEl = document.getElementById('flow-editor-root')
  const rawDef = mountEl?.dataset?.definition
  let initialDef = DEFAULT_FLOW
  try {
    const parsed = rawDef ? JSON.parse(rawDef) : null
    if (parsed && parsed.nodes && parsed.nodes.length > 0) initialDef = parsed
  } catch {}

  const { nodes: initNodes, edges: initEdges, entryNodeId: initEntry } = toReactFlow(initialDef)

  const [nodes, setNodes, onNodesChange] = useNodesState(initNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(initEdges)
  const [entryNodeId, setEntryNodeId] = useState(initEntry || initNodes[0]?.id || '')
  const [selectedNode, setSelectedNode] = useState(null)
  const [selectedEdge, setSelectedEdge] = useState(null)
  const [showJson, setShowJson] = useState(false)

  // Mark entry node in data
  const nodesWithEntry = useMemo(
    () => nodes.map((n) => ({ ...n, data: { ...n.data, isEntry: n.id === entryNodeId } })),
    [nodes, entryNodeId],
  )

  // Sync to hidden textarea on every change
  useEffect(() => {
    const ta = document.getElementById('definitionJson')
    if (!ta) return
    const def = fromReactFlow(nodesWithEntry, edges, entryNodeId)
    ta.value = JSON.stringify(def, null, 2)
  }, [nodesWithEntry, edges, entryNodeId])

  // Keep selectedNode in sync with nodes state
  useEffect(() => {
    if (!selectedNode) return
    const fresh = nodes.find((n) => n.id === selectedNode.id)
    if (fresh) setSelectedNode({ ...fresh, data: { ...fresh.data, isEntry: fresh.id === entryNodeId } })
  }, [nodes, entryNodeId])

  // Keep selectedEdge in sync with edges state
  useEffect(() => {
    if (!selectedEdge) return
    const fresh = edges.find((e) => e.id === selectedEdge.id)
    if (fresh) setSelectedEdge(fresh)
  }, [edges])

  const onConnect = useCallback(
    (params) => {
      const newEdge = {
        ...params,
        id: newId('e'),
        label: 'default',
        data: { condition: { type: 'default' } },
        style: { strokeWidth: 2 },
        labelStyle: { fontSize: 11, fill: '#374151', fontWeight: 500 },
        labelBgStyle: { fill: '#f9fafb', fillOpacity: 0.9 },
        labelBgPadding: [4, 4],
        labelBgBorderRadius: 4,
        markerEnd: { type: MarkerType.ArrowClosed, color: '#6b7280' },
      }
      setEdges((eds) => addEdge(newEdge, eds))
    },
    [setEdges],
  )

  const onNodeClick = useCallback((_evt, node) => {
    setSelectedEdge(null)
    setSelectedNode({ ...node, data: { ...node.data, isEntry: node.id === entryNodeId } })
  }, [entryNodeId])

  const onEdgeClick = useCallback((_evt, edge) => {
    setSelectedNode(null)
    setSelectedEdge(edge)
  }, [])

  const onPaneClick = useCallback(() => {
    setSelectedNode(null)
    setSelectedEdge(null)
  }, [])

  // Node panel callbacks
  const onNodeChange = useCallback((updated) => {
    setNodes((nds) => nds.map((n) => n.id === updated.id ? { ...n, data: updated.data } : n))
    setSelectedNode(updated)
  }, [setNodes])

  const onNodeDelete = useCallback((id) => {
    setNodes((nds) => nds.filter((n) => n.id !== id))
    setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id))
    setSelectedNode(null)
    if (entryNodeId === id) {
      setEntryNodeId(nodes.find((n) => n.id !== id)?.id || '')
    }
  }, [setNodes, setEdges, entryNodeId, nodes])

  // Edge panel callbacks
  const onEdgeChange = useCallback((updated) => {
    const newLabel = conditionLabel(updated.data?.condition)
    setEdges((eds) => eds.map((e) =>
      e.id === updated.id ? { ...e, data: updated.data, label: newLabel } : e
    ))
    setSelectedEdge({ ...updated, label: newLabel })
  }, [setEdges])

  const onEdgeDelete = useCallback((id) => {
    setEdges((eds) => eds.filter((e) => e.id !== id))
    setSelectedEdge(null)
  }, [setEdges])

  // Add a new node
  function addNode(type) {
    const id = newId('n')
    const cfg = NODE_CONFIGS[type]
    // Place near center of viewport, offset from existing nodes
    const x = 200 + (nodes.length % 3) * 250
    const y = 100 + Math.floor(nodes.length / 3) * 200
    const newNode = {
      id,
      type,
      position: { x, y },
      data: { label: cfg.label, config: {}, isEntry: false },
    }
    setNodes((nds) => [...nds, newNode])
    if (nodes.length === 0) setEntryNodeId(id)
  }

  const hasSelection = selectedNode || selectedEdge

  return (
    <div className="editor-shell">
      {/* Toolbar */}
      <div className="toolbar">
        <span className="toolbar-label">Add node</span>
        {Object.entries(NODE_CONFIGS).map(([type, cfg]) => (
          <button type="button"
            key={type}
            className="toolbar-btn"
            onClick={() => addNode(type)}
            title={`Add ${cfg.label} node`}
          >
            <span className="dot" style={{ background: cfg.color }} />
            {cfg.label}
          </button>
        ))}
        <div className="toolbar-sep" />
        {hasSelection && (
          <button type="button"
            className="toolbar-btn danger"
            onClick={() => {
              if (selectedNode) onNodeDelete(selectedNode.id)
              if (selectedEdge) onEdgeDelete(selectedEdge.id)
            }}
          >
            🗑 Delete selected
          </button>
        )}
      </div>

      {/* Canvas + side panel */}
      <div className="editor-canvas">
        <div className="rf-wrapper">
          <ReactFlow
            nodes={nodesWithEntry}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={onNodeClick}
            onEdgeClick={onEdgeClick}
            onPaneClick={onPaneClick}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            deleteKeyCode={null}
          >
            <Background color="#e5e7eb" gap={20} />
            <Controls />
            <MiniMap
              nodeColor={(n) => NODE_CONFIGS[n.type]?.color || '#6b7280'}
              style={{ background: '#f9fafb' }}
            />
          </ReactFlow>
        </div>

        {/* Side panel */}
        {selectedNode && (
          <NodePanel
            node={selectedNode}
            entryNodeId={entryNodeId}
            onChange={onNodeChange}
            onSetEntry={(id) => setEntryNodeId(id)}
            onDelete={onNodeDelete}
            onClose={() => setSelectedNode(null)}
          />
        )}
        {selectedEdge && !selectedNode && (
          <EdgePanel
            edge={selectedEdge}
            onChange={onEdgeChange}
            onDelete={onEdgeDelete}
            onClose={() => setSelectedEdge(null)}
          />
        )}
      </div>

      {/* JSON toggle footer */}
      <div className="json-toggle-row">
        <span>Flow syncs to JSON automatically.</span>
        <button type="button" className="json-toggle-btn" onClick={() => setShowJson((v) => !v)}>
          {showJson ? 'Hide JSON ▲' : 'View JSON ▼'}
        </button>
        {showJson && (
          <button type="button"
            className="json-toggle-btn"
            onClick={() => {
              const ta = document.getElementById('definitionJson')
              navigator.clipboard?.writeText(ta?.value || '')
            }}
          >
            Copy
          </button>
        )}
      </div>
    </div>
  )
}
