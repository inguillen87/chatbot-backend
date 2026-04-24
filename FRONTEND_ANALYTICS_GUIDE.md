# Frontend Analytics Implementation Guide

This guide details the implementation of the new Analytics Module on the frontend, connecting to the generic `/api/analytics` endpoints.

## 1. Service Layer (`src/services/analyticsService.ts`)

```typescript
import api from '../api/axiosConfig'; // Or your axios instance

export interface AnalyticsFilters {
  tenant_id?: number;
  from?: string; // ISO Date
  to?: string;   // ISO Date
  context?: 'overview' | 'municipio' | 'pyme';
  channel?: string;
}

export interface AnalyticsSummary {
  kpis: {
    total_interactions: number;
    active_users: number;
    avg_response_time_s: number;
    conversion_rate?: number;
    backlog_open?: number;
    sla_breaches?: number;
  };
  top_categories: { category: string; count: number }[];
  volume_by_day: { date: string; count: number }[];
  heatmap_points: { lat: number; lng: number; weight: number }[]; // optional here if fetched separately
  insights: any[];
}

export const analyticsService = {
  getSummary: async (filters: AnalyticsFilters): Promise<AnalyticsSummary> => {
    const params = new URLSearchParams();
    if (filters.tenant_id) params.append('tenant_id', filters.tenant_id.toString());
    if (filters.from) params.append('from', filters.from);
    if (filters.to) params.append('to', filters.to);
    if (filters.context) params.append('context', filters.context);
    if (filters.channel) params.append('channel', filters.channel);

    const response = await api.get(`/api/analytics/summary?${params.toString()}`);
    return response.data;
  },

  getHeatmap: async (filters: AnalyticsFilters) => {
    const params = new URLSearchParams();
    if (filters.tenant_id) params.append('tenant_id', filters.tenant_id.toString());
    if (filters.from) params.append('from', filters.from);
    if (filters.to) params.append('to', filters.to);

    const response = await api.get(`/api/analytics/heatmap?${params.toString()}`);
    return response.data.points; // Expects { points: [...] }
  },

  getInsights: async (tenantId: number) => {
    const response = await api.get(`/api/analytics/insights?tenant_id=${tenantId}`);
    return response.data.insights;
  }
};
```

## 2. Analytics Page (`src/pages/AnalyticsPage.tsx`)

Cleaned up structure to avoid duplicate imports and JSX return errors.

```tsx
import React, { useEffect, useState, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { Loader2, AlertCircle } from 'lucide-react';

import { analyticsService, AnalyticsSummary } from '../services/analyticsService';
import OverviewDashboard from '../components/analytics/OverviewDashboard';
import HeatmapDashboard from '../components/analytics/HeatmapDashboard';
import InsightsDashboard from '../components/analytics/InsightsDashboard';

const AnalyticsPage = () => {
  const [searchParams] = useSearchParams();
  const tenantIdStr = searchParams.get('tenant_id') || '1'; // Default or from context
  const tenantId = parseInt(tenantIdStr, 10);

  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [timeRange, setTimeRange] = useState('7d');
  const [context, setContext] = useState<'overview' | 'municipio' | 'pyme'>('overview');

  // Compute dates based on timeRange
  const dateRange = useMemo(() => {
    const to = new Date();
    const from = new Date();
    if (timeRange === '24h') from.setHours(from.getHours() - 24);
    if (timeRange === '7d') from.setDate(from.getDate() - 7);
    if (timeRange === '30d') from.setDate(from.getDate() - 30);
    return { from: from.toISOString(), to: to.toISOString() };
  }, [timeRange]);

  const fetchData = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await analyticsService.getSummary({
        tenant_id: tenantId,
        from: dateRange.from,
        to: dateRange.to,
        context: context
      });
      setData(result);
    } catch (err: any) {
      console.error(err);
      setError("No se pudo cargar el dashboard.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [tenantId, dateRange, context]);

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8 text-center text-red-500">
        <AlertCircle className="w-12 h-12 mx-auto mb-4" />
        <p>{error}</p>
        <Button onClick={fetchData} variant="outline" className="mt-4">Reintentar</Button>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6 bg-gray-50 min-h-screen">
      <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-gray-900">Analytics</h1>
          <p className="text-gray-500">Metricas en tiempo real</p>
        </div>

        <div className="flex items-center gap-2">
          <Select value={timeRange} onValueChange={setTimeRange}>
            <SelectTrigger className="w-[180px]">
              <SelectValue placeholder="Periodo" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="24h">Últimas 24 horas</SelectItem>
              <SelectItem value="7d">Últimos 7 días</SelectItem>
              <SelectItem value="30d">Últimos 30 días</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <Tabs defaultValue="overview" className="w-full" onValueChange={(val) => setContext(val as any)}>
        <TabsList className="grid w-full grid-cols-4 lg:w-[400px]">
          <TabsTrigger value="overview">General</TabsTrigger>
          <TabsTrigger value="municipio">Municipio</TabsTrigger>
          <TabsTrigger value="pyme">Ventas</TabsTrigger>
          <TabsTrigger value="geo">Mapas</TabsTrigger>
        </TabsList>

        <div className="mt-6">
          <TabsContent value="overview">
            {data && <OverviewDashboard data={data} />}
          </TabsContent>

          <TabsContent value="municipio">
            {data && <OverviewDashboard data={data} showSla={true} />}
          </TabsContent>

          <TabsContent value="pyme">
            {data && <OverviewDashboard data={data} showConversion={true} />}
          </TabsContent>

          <TabsContent value="geo">
            <HeatmapDashboard tenantId={tenantId} dateRange={dateRange} />
          </TabsContent>
        </div>
      </Tabs>

      {/* Insights Section always visible at bottom or side */}
      <div className="mt-8">
        <h2 className="text-xl font-semibold mb-4">Insights IA</h2>
        <InsightsDashboard tenantId={tenantId} />
      </div>
    </div>
  );
};

export default AnalyticsPage;
```

## 3. Overview Dashboard (`src/components/analytics/OverviewDashboard.tsx`)

```tsx
import React from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import { AnalyticsSummary } from '../../services/analyticsService';

interface Props {
  data: AnalyticsSummary;
  showSla?: boolean;
  showConversion?: boolean;
}

const OverviewDashboard: React.FC<Props> = ({ data, showSla, showConversion }) => {
  const { kpis, volume_by_day, top_categories } = data;

  return (
    <div className="space-y-6">
      {/* KPI Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Interacciones Totales</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{kpis.total_interactions}</div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Usuarios Activos</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{kpis.active_users}</div>
          </CardContent>
        </Card>

        {showConversion && (
          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">Tasa de Conversión</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{kpis.conversion_rate}%</div>
            </CardContent>
          </Card>
        )}

        {showSla && (
           <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">Backlog Abierto</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{kpis.backlog_open}</div>
            </CardContent>
          </Card>
        )}
      </div>

      {/* Charts Row */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Volumen Diario</CardTitle>
          </CardHeader>
          <CardContent className="h-[300px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={volume_by_day}>
                <XAxis dataKey="date" fontSize={12} tickLine={false} axisLine={false} />
                <YAxis fontSize={12} tickLine={false} axisLine={false} />
                <Tooltip />
                <Bar dataKey="count" fill="#3b82f6" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Top Categorías</CardTitle>
          </CardHeader>
          <CardContent className="h-[300px]">
             <div className="space-y-4">
                {top_categories.map((cat, idx) => (
                  <div key={idx} className="flex items-center">
                    <div className="w-full flex-1">
                        <div className="flex items-center justify-between mb-1">
                            <span className="text-sm font-medium">{cat.category}</span>
                            <span className="text-sm text-muted-foreground">{cat.count}</span>
                        </div>
                        <div className="h-2 w-full bg-secondary rounded-full overflow-hidden">
                            <div
                                className="h-full bg-primary"
                                style={{ width: `${(cat.count / Math.max(...top_categories.map(c=>c.count))) * 100}%` }}
                            />
                        </div>
                    </div>
                  </div>
                ))}
             </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default OverviewDashboard;
```

## 4. Heatmap Dashboard (`src/components/analytics/HeatmapDashboard.tsx`)

Requires `maplibre-gl` installed.

```tsx
import React, { useEffect, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { analyticsService } from '../../services/analyticsService';

interface Props {
  tenantId: number;
  dateRange: { from: string; to: string };
}

const HeatmapDashboard: React.FC<Props> = ({ tenantId, dateRange }) => {
  const mapContainer = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const [points, setPoints] = useState<any[]>([]);

  useEffect(() => {
    analyticsService.getHeatmap({
        tenant_id: tenantId,
        from: dateRange.from,
        to: dateRange.to
    }).then(data => setPoints(data || []));
  }, [tenantId, dateRange]);

  useEffect(() => {
    if (!mapContainer.current) return;
    if (map.current) return;

    map.current = new maplibregl.Map({
      container: mapContainer.current,
      style: 'https://demotiles.maplibre.org/style.json', // Replace with your style
      center: [-58.38, -34.60], // Buenos Aires default
      zoom: 11
    });

    map.current.on('load', () => {
        // Init logic
    });
  }, []);

  useEffect(() => {
    if (!map.current || !map.current.isStyleLoaded()) return;

    // Convert points to GeoJSON
    const geojson = {
        type: 'FeatureCollection',
        features: points.map(p => ({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [p.lng, p.lat] },
            properties: { weight: p.weight }
        }))
    };

    const sourceId = 'heatmap-source';
    const layerId = 'heatmap-layer';

    if (map.current.getSource(sourceId)) {
        (map.current.getSource(sourceId) as maplibregl.GeoJSONSource).setData(geojson as any);
    } else {
        map.current.addSource(sourceId, { type: 'geojson', data: geojson as any });
        map.current.addLayer({
            id: layerId,
            type: 'heatmap',
            source: sourceId,
            paint: {
                'heatmap-weight': ['get', 'weight'],
                'heatmap-intensity': 1,
                'heatmap-color': [
                    'interpolate', ['linear'], ['heatmap-density'],
                    0, 'rgba(33,102,172,0)',
                    0.2, 'rgb(103,169,207)',
                    0.4, 'rgb(209,229,240)',
                    0.6, 'rgb(253,219,199)',
                    0.8, 'rgb(239,138,98)',
                    1, 'rgb(178,24,43)'
                ],
                'heatmap-radius': 20,
                'heatmap-opacity': 0.8
            }
        });
    }

  }, [points]);

  return <div ref={mapContainer} className="w-full h-[500px] rounded-lg border shadow-sm" />;
};

export default HeatmapDashboard;
```

## 5. Insights Dashboard (`src/components/analytics/InsightsDashboard.tsx`)

```tsx
import React, { useEffect, useState } from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { Lightbulb, AlertTriangle, CheckCircle } from 'lucide-react';
import { analyticsService } from '../../services/analyticsService';

interface Props {
  tenantId: number;
}

const InsightsDashboard: React.FC<Props> = ({ tenantId }) => {
  const [insights, setInsights] = useState<any[]>([]);

  useEffect(() => {
    analyticsService.getInsights(tenantId).then(setInsights);
  }, [tenantId]);

  if (!insights.length) return null;

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
      {insights.map((insight, idx) => (
        <Card key={idx} className="border-l-4 border-l-yellow-400">
          <CardContent className="pt-6">
            <div className="flex items-start gap-3">
                {insight.severity === 'high' ? <AlertTriangle className="text-red-500" /> :
                 insight.severity === 'med' ? <Lightbulb className="text-yellow-500" /> :
                 <CheckCircle className="text-green-500" />}
                <div>
                    <p className="font-medium">{insight.text}</p>
                    <p className="text-xs text-muted-foreground mt-2">Confianza: {(insight.confidence * 100).toFixed(0)}%</p>
                </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
};

export default InsightsDashboard;
```
