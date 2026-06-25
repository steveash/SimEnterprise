import { useEffect, useRef } from 'react'
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape'
import fcose from 'cytoscape-fcose'
import dagre from 'cytoscape-dagre'
import { useStore } from '../store.js'
import { typeColor, HIERARCHY_EDGES } from '../constants.js'
import type { GraphModel } from '../../shared/model.js'

cytoscape.use(fcose)
cytoscape.use(dagre)

function buildElements(
  model: GraphModel,
  visibleTypes: Set<string>,
  visibleEdgeTypes: Set<string>,
  timeCursor: number | null
): ElementDefinition[] {
  const shown = new Set<string>()
  const els: ElementDefinition[] = []
  for (const n of model.nodes) {
    if (!visibleTypes.has(n.type)) continue
    const created = Date.parse(n.created_at)
    const future = timeCursor !== null && !Number.isNaN(created) && created > timeCursor
    shown.add(n.id)
    els.push({
      data: { id: n.id, label: n.label, ntype: n.type },
      classes: future ? 'future' : ''
    })
  }
  for (const e of model.edges) {
    if (!visibleEdgeTypes.has(e.type)) continue
    if (!shown.has(e.src) || !shown.has(e.dst)) continue
    els.push({
      data: { id: e.id, source: e.src, target: e.dst, etype: e.type, hierarchy: HIERARCHY_EDGES.has(e.type) ? 1 : 0 }
    })
  }
  return els
}

function layoutOptions(layout: string): cytoscape.LayoutOptions {
  if (layout === 'dagre') {
    return {
      name: 'dagre',
      rankDir: 'TB',
      nodeSep: 40,
      rankSep: 80,
      acyclicer: 'greedy'
    } as cytoscape.LayoutOptions
  }
  if (layout === 'concentric') {
    return { name: 'concentric', minNodeSpacing: 40, concentric: (n: cytoscape.NodeSingular) => n.degree(false), levelWidth: () => 2 } as cytoscape.LayoutOptions
  }
  return {
    name: 'fcose',
    quality: 'proof',
    animate: true,
    animationDuration: 400,
    nodeRepulsion: 8000,
    idealEdgeLength: 90,
    nodeSeparation: 120
  } as cytoscape.LayoutOptions
}

export function GraphView(): JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  const cyRef = useRef<Core | null>(null)

  const model = useStore((s) => s.model)
  const visibleTypes = useStore((s) => s.visibleTypes)
  const visibleEdgeTypes = useStore((s) => s.visibleEdgeTypes)
  const timeCursor = useStore((s) => s.timeCursor)
  const layout = useStore((s) => s.layout)
  const selectedId = useStore((s) => s.selectedId)
  const highlightNodes = useStore((s) => s.highlightNodes)
  const highlightEdges = useStore((s) => s.highlightEdges)
  const focus = useStore((s) => s.focus)
  const select = useStore((s) => s.select)

  // create the instance once
  useEffect(() => {
    if (!ref.current) return
    const cy = cytoscape({
      container: ref.current,
      minZoom: 0.1,
      maxZoom: 3,
      wheelSensitivity: 0.2,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': (ele: cytoscape.NodeSingular) => typeColor(ele.data('ntype')),
            label: 'data(label)',
            color: '#e8eef5',
            'font-size': 9,
            'text-wrap': 'wrap',
            'text-max-width': '90px',
            'text-valign': 'bottom',
            'text-margin-y': 3,
            width: 22,
            height: 22,
            'border-width': 0,
            'transition-property': 'opacity, border-width, width, height',
            'transition-duration': 150
          }
        },
        {
          selector: 'edge',
          style: {
            width: 1.2,
            'line-color': '#3a4654',
            'target-arrow-color': '#3a4654',
            'target-arrow-shape': 'triangle',
            'arrow-scale': 0.7,
            'curve-style': 'bezier',
            opacity: 0.55
          }
        },
        { selector: 'node.future', style: { opacity: 0.12 } },
        { selector: 'edge.future', style: { opacity: 0.05 } },
        {
          selector: 'node.hl',
          style: { 'border-width': 4, 'border-color': '#ffd166', width: 30, height: 30, 'z-index': 10 }
        },
        { selector: 'edge.hl', style: { 'line-color': '#ffd166', 'target-arrow-color': '#ffd166', width: 3, opacity: 1, 'z-index': 9 } },
        { selector: 'node.dim', style: { opacity: 0.15 } },
        { selector: 'edge.dim', style: { opacity: 0.05 } },
        {
          selector: 'node.sel',
          style: { 'border-width': 4, 'border-color': '#06d6a0', 'z-index': 11 }
        },
        { selector: 'edge[hierarchy = 1]', style: { 'line-color': '#52606d', width: 1.6, opacity: 0.7 } }
      ]
    })
    cy.on('tap', 'node', (ev) => select(ev.target.id()))
    cy.on('tap', (ev) => {
      if (ev.target === cy) select(null)
    })
    cyRef.current = cy
    return () => {
      cy.destroy()
      cyRef.current = null
    }
  }, [select])

  // rebuild elements + run layout when data / filters change
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !model) return
    cy.batch(() => {
      cy.elements().remove()
      cy.add(buildElements(model, visibleTypes, visibleEdgeTypes, timeCursor))
    })
    cy.layout(layoutOptions(layout)).run()
    cy.fit(undefined, 40)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, visibleTypes, visibleEdgeTypes, layout])

  // time cursor only toggles a class (no relayout)
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !model) return
    const createdAt = new Map(model.nodes.map((n) => [n.id, Date.parse(n.created_at)]))
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const created = createdAt.get(n.id()) ?? NaN
        const future = timeCursor !== null && !Number.isNaN(created) && created > timeCursor
        n.toggleClass('future', future)
      })
    })
  }, [timeCursor, model])

  // highlight + selection classes
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    const hasHl = highlightNodes.size > 0 || highlightEdges.size > 0
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const hl = highlightNodes.has(n.id())
        n.toggleClass('hl', hl)
        n.toggleClass('dim', hasHl && !hl)
        n.toggleClass('sel', n.id() === selectedId)
      })
      cy.edges().forEach((e) => {
        const hl = highlightEdges.has(e.id()) || (highlightNodes.has(e.source().id()) && highlightNodes.has(e.target().id()))
        e.toggleClass('hl', hl)
        e.toggleClass('dim', hasHl && !hl)
      })
    })
  }, [highlightNodes, highlightEdges, selectedId])

  // focus request -> fit/zoom
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || !focus) return
    if (focus.nodeIds.length) {
      let coll = cy.collection()
      for (const id of focus.nodeIds) coll = coll.union(cy.getElementById(id))
      if (coll.length) cy.animate({ fit: { eles: coll, padding: 80 }, duration: 400 })
    } else if (focus.fit) {
      cy.animate({ fit: { eles: cy.elements(), padding: 40 }, duration: 300 })
    }
  }, [focus])

  return <div className="graph-canvas" ref={ref} />
}
