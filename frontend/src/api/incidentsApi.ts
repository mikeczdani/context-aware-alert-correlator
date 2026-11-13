/**
 * api/incidentsApi.ts – REST fetch függvények és React Query kulcsok
 *
 * Ez a fájl tartalmazza az összes backend API hívást.
 * A TanStack Query useQuery hookjai ezeket a függvényeket hívják
 * a queryFn paraméterként.
 */

import type { CorrelatedIncident, KialiGraph, SystemMetrics } from '../types/api'

const BASE_URL = '/api/v1'

// ---------------------------------------------------------------------------
// Fetch függvények
// ---------------------------------------------------------------------------

export async function fetchIncidents(): Promise<CorrelatedIncident[]> {
  const res = await fetch(`${BASE_URL}/incidents`)
  if (!res.ok) throw new Error('Failed to fetch incidents')
  return res.json()
}

export async function fetchMetrics(): Promise<SystemMetrics> {
  const res = await fetch(`${BASE_URL}/metrics/summary`)
  if (!res.ok) throw new Error('Failed to fetch metrics')
  return res.json()
}

export async function fetchTopology(): Promise<KialiGraph> {
  const res = await fetch(`${BASE_URL}/topology/current`)
  if (!res.ok) throw new Error('Failed to fetch topology')
  return res.json()
}

export async function runSimulation(scenarioId: string): Promise<{ incident_id: string; summary: string }> {
  const res = await fetch(`${BASE_URL}/simulate/${scenarioId}`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to run simulation')
  return res.json()
}

export async function resetAll(): Promise<void> {
  const res = await fetch(`${BASE_URL}/incidents/reset`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to reset')
}
