import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type PresetRef } from '../api/client';
import { findPreset } from '../utils/slicePresetPicker';
import { buildCompatibilityIndex } from '../utils/slicerPrinterMatch';
import { BedTypeDropdown, PresetDropdown } from './SliceModal';

export interface GotoPrintProfile {
  printer: PresetRef | null;
  process: PresetRef | null;
  bedType: string | null;
}

/**
 * The printer's go-to print profile for the MakerWorld one-tap print flow:
 * printer + process preset and the build plate actually installed. Uses the
 * Slice dialog's own dropdowns, so the lists and compatibility filtering
 * match it.
 */
export function GotoPrintProfileFields({
  value,
  onChange,
}: {
  value: GotoPrintProfile;
  onChange: (value: GotoPrintProfile) => void;
}) {
  const { t } = useTranslation();
  const presetsQuery = useQuery({ queryKey: ['slicerPresets'], queryFn: () => api.getSlicerPresets() });
  const printerModelsQuery = useQuery({
    queryKey: ['slicerPrinterModels'],
    queryFn: api.getSlicerPrinterModels,
    staleTime: Infinity,
  });
  const compatIndex = useMemo(() => buildCompatibilityIndex(printerModelsQuery.data ?? {}), [printerModelsQuery.data]);
  const data = presetsQuery.data;

  return (
    <fieldset className="space-y-3 border-t border-bambu-dark-tertiary pt-4">
      <legend className="text-sm font-medium text-white">{t('printers.gotoProfile.title')}</legend>
      <p className="text-xs text-bambu-gray">{t('printers.gotoProfile.help')}</p>
      {data ? (
        <>
          <PresetDropdown
            label={t('printers.gotoProfile.printer')}
            slot="printer"
            data={data}
            value={value.printer}
            onChange={(printer) => onChange({ ...value, printer })}
          />
          <PresetDropdown
            label={t('printers.gotoProfile.process')}
            slot="process"
            data={data}
            value={value.process}
            onChange={(process) => onChange({ ...value, process })}
            selectedPrinterName={findPreset(data, value.printer, 'printer')?.name ?? null}
            compatIndex={compatIndex}
          />
        </>
      ) : (
        <p className="text-xs text-bambu-gray">{t('printers.gotoProfile.loading')}</p>
      )}
      <BedTypeDropdown value={value.bedType} onChange={(bedType) => onChange({ ...value, bedType })} />
    </fieldset>
  );
}
