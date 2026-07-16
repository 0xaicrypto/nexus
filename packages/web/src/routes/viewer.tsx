import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { ArrowLeft, ChevronLeft, ChevronRight, Image as ImageIcon, Layers } from 'lucide-react';
import { api, ApiError } from '@/lib/api-client';
import { Alert, Badge, Button, Card, Skeleton } from '@/components/ui';
import { cn } from '@/lib/utils';

interface StudyDetail {
  study_id: string;
  modality: string;
  body_part?: string;
  series_count: number;
  slice_count?: number;
  created_at: string;
  series?: Array<{
    series_uid: string;
    series_description?: string;
    slice_count: number;
  }>;
}

interface SeriesThumbnail {
  series_uid: string;
  url: string;
  error?: string;
}

const WINDOW_PRESETS: Array<{ label: string; key: string }> = [
  { label: 'Lung', key: 'lung' },
  { label: 'Bone', key: 'bone' },
  { label: 'Soft Tissue', key: 'soft-tissue' },
  { label: 'Brain', key: 'brain' },
];

const modalityColors: Record<string, string> = {
  CT: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  MR: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  XR: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  US: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  NM: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400',
  PT: 'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400',
  CR: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
  DX: 'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-400',
  MG: 'bg-pink-100 text-pink-700 dark:bg-pink-900/30 dark:text-pink-400',
};

export function ViewerPage() {
  const { studyId } = useParams<{ studyId: string }>();
  const [study, setStudy] = useState<StudyDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [thumbnails, setThumbnails] = useState<SeriesThumbnail[]>([]);
  const [thumbsLoading, setThumbsLoading] = useState(false);

  const [expandedSeriesIdx, setExpandedSeriesIdx] = useState<number | null>(null);
  const [sliceIndex, setSliceIndex] = useState(0);
  const [activePreset, setActivePreset] = useState<string | null>(null);
  const [sliceImageUrl, setSliceImageUrl] = useState<string | null>(null);
  const [sliceLoading, setSliceLoading] = useState(false);

  const loadStudy = useCallback(() => {
    if (!studyId) return;
    setLoading(true);
    setError(null);
    api
      .getDicomStudy(studyId)
      .then((data) => {
        setStudy(data);
        if (data.series && data.series.length > 0) {
          setThumbsLoading(true);
          Promise.all(
            data.series.map(async (s) => {
              try {
                const blob = await api.renderDicomSlice(studyId, data.series!.indexOf(s), 0);
                return { series_uid: s.series_uid, url: URL.createObjectURL(blob) };
              } catch {
                return { series_uid: s.series_uid, url: '', error: 'Failed to load thumbnail' };
              }
            }),
          )
            .then(setThumbnails)
            .finally(() => setThumbsLoading(false));
        }
      })
      .catch((err) => setError(err instanceof ApiError ? err.messageText : String(err)))
      .finally(() => setLoading(false));
  }, [studyId]);

  useEffect(() => {
    loadStudy();
  }, [loadStudy]);

  useEffect(() => {
    return () => {
      thumbnails.forEach((t) => {
        if (t.url) URL.revokeObjectURL(t.url);
      });
    };
  }, [thumbnails]);

  const loadSliceImage = useCallback(async (seriesIdx: number, idx: number, preset: string | null) => {
    if (!studyId) return;
    setSliceLoading(true);
    setSliceImageUrl(null);
    try {
      const blob = await api.renderDicomSlice(studyId, seriesIdx, idx, preset || undefined);
      const url = URL.createObjectURL(blob);
      setSliceImageUrl((prev) => { if (prev) URL.revokeObjectURL(prev); return url; });
    } catch {
      setSliceImageUrl(null);
    } finally {
      setSliceLoading(false);
    }
  }, [studyId]);

  useEffect(() => {
    return () => { if (sliceImageUrl) URL.revokeObjectURL(sliceImageUrl); };
  }, [sliceImageUrl]);

  const handleSeriesClick = (idx: number) => {
    if (expandedSeriesIdx === idx) {
      setExpandedSeriesIdx(null);
      setSliceImageUrl(null);
      return;
    }
    setExpandedSeriesIdx(idx);
    setSliceIndex(0);
    loadSliceImage(idx, 0, activePreset);
  };

  const handleSliceChange = (delta: number) => {
    if (expandedSeriesIdx === null || !study?.series) return;
    const series = study.series[expandedSeriesIdx];
    const newIdx = Math.max(0, Math.min(series.slice_count - 1, sliceIndex + delta));
    setSliceIndex(newIdx);
    loadSliceImage(expandedSeriesIdx, newIdx, activePreset);
  };

  const handlePreset = (key: string) => {
    const next = activePreset === key ? null : key;
    setActivePreset(next);
    if (expandedSeriesIdx !== null) {
      loadSliceImage(expandedSeriesIdx, sliceIndex, next);
    }
  };

  const colorClass = study ? (modalityColors[study.modality] || 'bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400') : '';

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Alert variant="error">{error}</Alert>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="flex items-center gap-4 border-b border-border bg-surface px-6 py-3">
        <Button size="sm" variant="ghost" onClick={() => window.history.back()}>
          <ArrowLeft size={16} />
        </Button>
        {loading ? (
          <Skeleton className="h-6 w-64" />
        ) : (
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold text-text-primary">DICOM Viewer</h1>
            {study && (
              <>
                <span className={cn('rounded-md px-2 py-0.5 text-xs font-semibold', colorClass)}>
                  {study.modality}
                </span>
                <span className="text-sm text-text-secondary">
                  {study.body_part || ''}
                </span>
              </>
            )}
          </div>
        )}
      </div>

      {loading ? (
        <div className="space-y-4 p-6">
          <Skeleton className="h-8 w-full" />
          <div className="grid grid-cols-3 gap-4">
            <Skeleton className="h-48 rounded-xl" />
            <Skeleton className="h-48 rounded-xl" />
            <Skeleton className="h-48 rounded-xl" />
          </div>
        </div>
      ) : study ? (
        <div className="p-6">
          <Card className="mb-6 p-4">
            <div className="grid grid-cols-4 gap-4 text-sm">
              <div>
                <p className="text-xs text-text-tertiary">Modality</p>
                <p className="font-medium text-text-primary">{study.modality}</p>
              </div>
              <div>
                <p className="text-xs text-text-tertiary">Body Part</p>
                <p className="font-medium text-text-primary">{study.body_part || '—'}</p>
              </div>
              <div>
                <p className="text-xs text-text-tertiary">Series</p>
                <p className="font-medium text-text-primary">{study.series_count}</p>
              </div>
              <div>
                <p className="text-xs text-text-tertiary">Slices</p>
                <p className="font-medium text-text-primary">{study.slice_count || '—'}</p>
              </div>
            </div>
          </Card>

          {expandedSeriesIdx !== null && study.series && (
            <Card className="mb-6 overflow-hidden">
              <div className="flex items-center justify-between border-b border-border bg-surface px-4 py-2">
                <div className="flex items-center gap-2">
                  <h3 className="text-sm font-semibold text-text-primary">
                    {study.series[expandedSeriesIdx].series_description || `Series ${expandedSeriesIdx + 1}`}
                  </h3>
                  <Badge variant="default">
                    Slice {sliceIndex + 1} / {study.series[expandedSeriesIdx].slice_count}
                  </Badge>
                </div>
                <div className="flex items-center gap-2">
                  <div className="flex gap-1">
                    {WINDOW_PRESETS.map((p) => (
                      <Button
                        key={p.key}
                        size="sm"
                        variant={activePreset === p.key ? 'primary' : 'secondary'}
                        onClick={() => handlePreset(p.key)}
                      >
                        {p.label}
                      </Button>
                    ))}
                  </div>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => handleSliceChange(-1)}
                    disabled={sliceIndex === 0}
                  >
                    <ChevronLeft size={14} />
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => handleSliceChange(1)}
                    disabled={sliceIndex >= study.series[expandedSeriesIdx].slice_count - 1}
                  >
                    <ChevronRight size={14} />
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setExpandedSeriesIdx(null)}>
                    ✕
                  </Button>
                </div>
              </div>
              <div className="flex items-center justify-center bg-black p-4" style={{ minHeight: '400px' }}>
                {sliceLoading ? (
                  <Skeleton className="h-96 w-full" />
                ) : sliceImageUrl ? (
                  <img src={sliceImageUrl} alt={`Slice ${sliceIndex + 1}`} className="max-h-[600px] object-contain" />
                ) : (
                  <div className="flex flex-col items-center gap-2">
                    <ImageIcon size={48} className="text-text-tertiary" />
                    <p className="text-sm text-text-tertiary">Failed to load slice</p>
                  </div>
                )}
              </div>
            </Card>
          )}

          <div className="mb-4 flex items-center gap-2">
            <Layers size={18} className="text-text-secondary" />
            <h2 className="text-lg font-semibold text-text-primary">Series</h2>
            <Badge variant="default">{study.series_count}</Badge>
          </div>

          {thumbsLoading ? (
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
              <Skeleton className="h-48 rounded-xl" />
              <Skeleton className="h-48 rounded-xl" />
              <Skeleton className="h-48 rounded-xl" />
            </div>
          ) : thumbnails.length === 0 ? (
            <Card className="flex flex-col items-center justify-center p-12 text-center">
              <ImageIcon size={40} className="mb-3 text-text-tertiary" />
              <p className="text-sm text-text-secondary">No series thumbnails available</p>
            </Card>
          ) : (
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
              {thumbnails.map((t, i) => {
                const series = study.series?.[i];
                return (
                <div
                  key={t.series_uid}
                  className={cn(
                    'rounded-xl border border-border bg-surface-elevated shadow-sm overflow-hidden cursor-pointer transition-all',
                    expandedSeriesIdx === i && 'ring-2 ring-accent',
                  )}
                    onClick={() => handleSeriesClick(i)}
                  >
                    {t.url ? (
                      <img
                        src={t.url}
                        alt={`Series ${i + 1}`}
                        className="h-48 w-full object-contain bg-black"
                      />
                    ) : (
                      <div className="flex h-48 w-full items-center justify-center bg-surface">
                        <ImageIcon size={32} className="text-text-tertiary" />
                      </div>
                    )}
                    <div className="p-3">
                      <p className="truncate text-sm font-medium text-text-primary">
                        {series?.series_description || `Series ${i + 1}`}
                      </p>
                      <p className="text-xs text-text-tertiary">
                        {series?.slice_count || 0} slices · UID: {t.series_uid.slice(0, 12)}...
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
