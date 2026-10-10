import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, Printer as PrinterIcon, X } from 'lucide-react';
import {
  api,
  ApiError,
  type MakerworldPrintResult,
  type SliceJobState,
} from '../api/client';
import { buildLoadedFilaments } from '../hooks/useFilamentMapping';
import { useToast } from '../contexts/ToastContext';
import { formatDuration } from '../utils/date';
import { Button } from './Button';

interface PrinterChoice {
  id: number;
  name: string;
  model: string | null;
}

type Step =
  | { kind: 'starting' }
  | { kind: 'pickPrinter'; printers: PrinterChoice[] }
  | { kind: 'slicing'; jobId: number; job: SliceJobState | null }
  | { kind: 'preview'; result: MakerworldPrintResult }
  | { kind: 'error'; message: string };

interface MakerworldPrintFlowProps {
  /** MakerWorld model URL, including any #profileId-… fragment. */
  url: string;
  printerId?: number | null;
  /** Resume an already-started flow (e.g. back from the G-code preview)
   *  instead of importing and slicing again. */
  jobId?: number | null;
  /** Called once the slice job exists, so the page can put it in the URL. */
  onJobStarted?: (jobId: number) => void;
  onClose: () => void;
}

/**
 * One-tap "print from a MakerWorld link": resolve → import → slice (a
 * background slice job, progress over the WebSocket) → preview → queue on
 * confirm. Full-screen on phones, a centred sheet from `md` up.
 */
export function MakerworldPrintFlow({ url, printerId, jobId: resumeJobId, onJobStarted, onClose }: MakerworldPrintFlowProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [step, setStep] = useState<Step>(
    resumeJobId ? { kind: 'slicing', jobId: resumeJobId, job: null } : { kind: 'starting' },
  );

  const start = useCallback(
    async (chosenPrinterId?: number | null) => {
      setStep({ kind: 'starting' });
      try {
        const started = await api.startMakerworldPrint(url, chosenPrinterId);
        setStep({ kind: 'slicing', jobId: started.job_id, job: null });
        onJobStarted?.(started.job_id);
      } catch (err) {
        if (err instanceof ApiError && err.code === 'printer_required') {
          setStep({ kind: 'pickPrinter', printers: (err.detail?.printers as PrinterChoice[]) ?? [] });
        } else {
          setStep({ kind: 'error', message: err instanceof Error ? err.message : String(err) });
        }
      }
    },
    [url, onJobStarted],
  );

  useEffect(() => {
    if (!resumeJobId) start(printerId);
    // Mount-only: a resumed job id must not restart the flow.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const jobId = step.kind === 'slicing' ? step.jobId : null;
  useEffect(() => {
    if (jobId === null) return;
    const apply = (job: SliceJobState) => {
      if (job.job_id !== jobId) return;
      setStep((prev) => {
        if (prev.kind !== 'slicing') return prev;
        if (job.status === 'completed') {
          return { kind: 'preview', result: job.result as MakerworldPrintResult };
        }
        if (job.status === 'failed') {
          return { kind: 'error', message: job.error_detail ?? t('makerworld.printFlow.sliceFailed') };
        }
        return { ...prev, job };
      });
    };
    const onEvent = (e: Event) => apply((e as CustomEvent<SliceJobState>).detail);
    window.addEventListener('bambuddy:slice-job', onEvent);
    // One fetch covers a job that finished before the listener was attached;
    // the slow poll only matters when the WebSocket is down.
    const poll = () => api.getSliceJob(jobId).then(apply).catch(() => undefined);
    poll();
    const id = window.setInterval(poll, 10000);
    return () => {
      window.removeEventListener('bambuddy:slice-job', onEvent);
      window.clearInterval(id);
    };
  }, [jobId, t]);

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex md:items-center md:justify-center md:p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t('makerworld.printFlow.title')}
        className="bg-bambu-dark-secondary w-full h-full md:h-auto md:max-h-[90vh] md:max-w-lg md:rounded-xl overflow-y-auto flex flex-col"
      >
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white flex items-center gap-2">
            <PrinterIcon className="w-5 h-5 text-bambu-green" />
            {t('makerworld.printFlow.title')}
          </h2>
          <button type="button" onClick={onClose} className="p-2 text-bambu-gray hover:text-white" aria-label={t('common.close')}>
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4 flex-1">
          {step.kind === 'starting' && <Busy label={t('makerworld.printFlow.preparing')} />}
          {step.kind === 'slicing' && (
            <Busy
              label={step.job?.progress?.stage ?? t('makerworld.printFlow.slicing')}
              percent={step.job?.progress?.total_percent}
            />
          )}
          {step.kind === 'pickPrinter' && (
            <div className="space-y-2">
              <p className="text-sm text-bambu-gray">{t('makerworld.printFlow.pickPrinter')}</p>
              {step.printers.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => start(p.id)}
                  className="w-full text-left p-3 rounded-lg bg-bambu-dark hover:bg-bambu-dark-tertiary text-white"
                >
                  {p.name}
                  {p.model && <span className="text-bambu-gray text-sm"> · {p.model}</span>}
                </button>
              ))}
            </div>
          )}
          {step.kind === 'error' && (
            <div className="space-y-4">
              <p className="text-red-400 text-sm whitespace-pre-line break-words">{step.message}</p>
              <Button variant="secondary" onClick={() => start(printerId)}>
                {t('makerworld.printFlow.retry')}
              </Button>
            </div>
          )}
          {step.kind === 'preview' && (
            <Preview
              result={step.result}
              onDone={() => {
                showToast(t('makerworld.printFlow.queued'), 'success');
                onClose();
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function Busy({ label, percent }: { label: string; percent?: number }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 text-bambu-gray">
      <Loader2 className="w-8 h-8 animate-spin text-bambu-green" />
      <span className="text-sm text-center">
        {label}
        {percent != null && percent > 0 && ` (${Math.round(percent)}%)`}
      </span>
    </div>
  );
}

function Preview({ result, onDone }: { result: MakerworldPrintResult; onDone: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [plateIdx, setPlateIdx] = useState(0);
  const [queueing, setQueueing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: platesData } = useQuery({
    queryKey: ['library-file-plates', result.library_file_id],
    queryFn: () => api.getLibraryFilePlates(result.library_file_id),
  });
  const { data: status } = useQuery({
    queryKey: ['printerStatus', result.printer_id],
    queryFn: () => api.getPrinterStatus(result.printer_id),
  });

  const flowPlate = result.plates[plateIdx] ?? result.plates[0];
  const plateNum = flowPlate?.plate ?? 1;
  const meta = platesData?.plates.find((p) => p.index === plateNum);
  const loaded = buildLoadedFilaments(status);

  const queue = async () => {
    setQueueing(true);
    setError(null);
    try {
      await api.addToQueue({
        printer_id: result.printer_id,
        library_file_id: result.library_file_id,
        plate_id: plateNum,
        ams_mapping: flowPlate?.ams_mapping ?? null,
      });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setQueueing(false);
    }
  };

  return (
    <div className="space-y-4">
      {result.plates.length > 1 && (
        <div className="flex flex-wrap gap-2">
          {result.plates.map((p, i) => (
            <button
              key={p.plate}
              type="button"
              onClick={() => setPlateIdx(i)}
              className={`px-3 py-1 rounded-full text-sm ${i === plateIdx ? 'bg-bambu-green text-white' : 'bg-bambu-dark text-bambu-gray'}`}
            >
              {t('makerworld.printFlow.plate', { n: p.plate })}
            </button>
          ))}
        </div>
      )}
      <img
        src={api.getLibraryFilePlateThumbnail(result.library_file_id, plateNum)}
        alt={t('makerworld.printFlow.plate', { n: plateNum })}
        className="w-full max-w-xs mx-auto aspect-square object-contain rounded-lg bg-bambu-dark"
      />
      <dl className="grid grid-cols-2 gap-2 text-sm">
        <dt className="text-bambu-gray">{t('makerworld.printFlow.printTime')}</dt>
        <dd className="text-white text-right">{formatDuration(meta?.print_time_seconds ?? result.print_time_seconds)}</dd>
        <dt className="text-bambu-gray">{t('makerworld.printFlow.plateType')}</dt>
        <dd className="text-white text-right break-words">{meta?.bed_type ?? '—'}</dd>
      </dl>
      <div>
        <h3 className="text-sm font-medium text-white mb-2">{t('makerworld.printFlow.filaments')}</h3>
        <ul className="space-y-1">
          {(meta?.filaments ?? []).map((f) => {
            const tray = flowPlate?.ams_mapping?.[f.slot_id - 1];
            const slot = loaded.find((l) => l.globalTrayId === tray);
            return (
              <li key={f.slot_id} className="flex items-center gap-2 text-sm">
                <span className="w-4 h-4 rounded-full border border-white/20 shrink-0" style={{ backgroundColor: f.color }} />
                <span className="text-white flex-1 min-w-0 truncate">
                  {f.type} → {slot ? slot.label : t('makerworld.printFlow.unmapped')}
                </span>
                <span className="text-bambu-gray shrink-0">{f.used_grams.toFixed(1)} g</span>
              </li>
            );
          })}
        </ul>
      </div>
      {error && <p className="text-red-400 text-sm break-words">{error}</p>}
      <div className="flex flex-wrap gap-2 pt-2">
        <Button
          variant="secondary"
          className="flex-1 min-w-[8rem]"
          onClick={() => navigate(`/gcode-viewer?library_file=${result.library_file_id}&plate=${plateNum}`)}
        >
          {t('makerworld.printFlow.gcodePreview')}
        </Button>
        <Button className="flex-1 min-w-[8rem]" onClick={queue} disabled={queueing}>
          {queueing ? <Loader2 className="w-4 h-4 animate-spin" /> : t('makerworld.printFlow.print')}
        </Button>
      </div>
    </div>
  );
}
