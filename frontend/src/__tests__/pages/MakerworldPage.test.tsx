/**
 * Tests for the MakerworldPage URL-paste flow.
 *
 * Covers: status-driven warning banner, resolve round-trip populates design +
 * plate list, "Already imported" badge appears when the backend reports prior
 * imports, button labels (Save / Save & Slice in <slicer>), URL-change detection,
 * inline imported-plate action buttons, the Recent imports sidebar, and
 * DOMPurify sanitising of user-authored summary HTML.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';

// The toolpath viewer needs WebGL; the flow tests only care where it opens.
vi.mock('../../components/GcodeToolpathViewer', () => ({
  GcodeToolpathViewer: ({ gcodeUrl }: { gcodeUrl: string }) => <div data-testid="gcode-viewer">{gcodeUrl}</div>,
}));
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { MakerworldPage } from '../../pages/MakerworldPage';

const statusNoToken = { has_cloud_token: false, can_download: false };
const statusWithToken = { has_cloud_token: true, can_download: true };

function resolveResponse(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    model_id: 1400373,
    profile_id: 1452154,
    design: {
      id: 1400373,
      title: 'Seed Starter',
      designCreator: { name: 'Meyui', avatar: '' },
      coverUrl: 'https://makerworld.bblmw.com/img/cover.png',
      license: 'Standard',
      downloadCount: 1234,
      summary: '<p>A seed starter</p>',
    },
    instances: [
      {
        id: 1452154,
        profileId: 298919107,
        title: '9 cells',
        cover: '',
        materialCnt: 1,
        needAms: false,
        downloadCount: 500,
      },
      {
        id: 1452158,
        profileId: 298919564,
        title: '12 cells',
        cover: '',
        materialCnt: 2,
        needAms: true,
        downloadCount: 120,
      },
    ],
    already_imported_library_ids: [],
    ...overrides,
  };
}

// Helper: seed all the handlers a "signed-in, happy-path" render needs.
// Individual tests layer extra handlers on top via ``server.use``.
function useAuthedHandlers(opts: {
  slicer?: 'bambu_studio' | 'orcaslicer';
  openInSlicer?: 'bambu_studio' | 'orcaslicer' | null;
  useSlicerApi?: boolean;
  recent?: Array<Record<string, unknown>>;
} = {}) {
  const slicer = opts.slicer ?? 'bambu_studio';
  const recent = opts.recent ?? [];
  server.use(
    http.get('*/makerworld/status', () => HttpResponse.json(statusWithToken)),
    http.get('*/makerworld/recent-imports', () => HttpResponse.json(recent)),
    http.get('*/library/folders', () => HttpResponse.json([])),
    http.get('*/settings/', () =>
      HttpResponse.json({
        auto_archive: true,
        save_thumbnails: true,
        preferred_slicer: slicer,
        ...(opts.openInSlicer !== undefined ? { open_in_slicer: opts.openInSlicer } : {}),
        ...(opts.useSlicerApi !== undefined ? { use_slicer_api: opts.useSlicerApi } : {}),
      }),
    ),
  );
}

afterEach(() => server.resetHandlers());

describe('MakerworldPage', () => {
  it('renders the sign-in-required banner when no Bambu Cloud token is stored', async () => {
    server.use(
      http.get('*/makerworld/status', () => HttpResponse.json(statusNoToken)),
    );
    render(<MakerworldPage />);
    expect(await screen.findByText(/Bambu Cloud sign-in required/i)).toBeInTheDocument();
  });

  it('hides the sign-in banner when a cloud token is present', async () => {
    useAuthedHandlers();
    render(<MakerworldPage />);
    // Page header is always visible; wait for status to settle
    await screen.findByRole('heading', { name: 'MakerWorld' });
    await waitFor(() => {
      expect(screen.queryByText(/Bambu Cloud sign-in required/i)).not.toBeInTheDocument();
    });
  });

  it('renders design + plate list after the user pastes a URL', async () => {
    useAuthedHandlers();
    server.use(
      http.post('*/makerworld/resolve', async ({ request }) => {
        const body = (await request.json()) as { url: string };
        expect(body.url).toContain('makerworld.com/en/models/1400373');
        return HttpResponse.json(resolveResponse());
      }),
    );
    render(<MakerworldPage />);

    const input = await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i);
    await userEvent.type(input, 'https://makerworld.com/en/models/1400373-slug#profileId-1452154');
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    expect(await screen.findByText('Seed Starter')).toBeInTheDocument();
    expect(screen.getByText('9 cells')).toBeInTheDocument();
    expect(screen.getByText('12 cells')).toBeInTheDocument();
  });

  it('shows the "Already in library" badge when backend reports prior imports', async () => {
    useAuthedHandlers();
    server.use(
      http.post('*/makerworld/resolve', () =>
        HttpResponse.json(resolveResponse({ already_imported_library_ids: [42] })),
      ),
    );
    render(<MakerworldPage />);
    await userEvent.type(
      await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i),
      'https://makerworld.com/en/models/1400373',
    );
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));
    expect(await screen.findByText(/Already in library/i)).toBeInTheDocument();
  });

  it('labels the per-plate import button "Save" (not "Import")', async () => {
    useAuthedHandlers();
    server.use(
      http.post('*/makerworld/resolve', () => HttpResponse.json(resolveResponse())),
    );
    render(<MakerworldPage />);
    await userEvent.type(
      await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i),
      'https://makerworld.com/en/models/1400373',
    );
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    await screen.findByText('Seed Starter');
    // Two plates → two Save buttons.
    const saveButtons = await screen.findAllByRole('button', { name: /^Save$/ });
    expect(saveButtons.length).toBe(2);
  });

  it('interpolates the slicer name into the slice button (Bambu Studio by default)', async () => {
    useAuthedHandlers({ slicer: 'bambu_studio' });
    server.use(
      http.post('*/makerworld/resolve', () => HttpResponse.json(resolveResponse())),
    );
    render(<MakerworldPage />);
    await userEvent.type(
      await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i),
      'https://makerworld.com/en/models/1400373',
    );
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    const sliceButtons = await screen.findAllByRole('button', {
      name: /Save & Slice in Bambu Studio/,
    });
    expect(sliceButtons.length).toBe(2);
  });

  it('interpolates OrcaSlicer when that is the configured preferred slicer', async () => {
    useAuthedHandlers({ slicer: 'orcaslicer' });
    server.use(
      http.post('*/makerworld/resolve', () => HttpResponse.json(resolveResponse())),
    );
    render(<MakerworldPage />);
    await userEvent.type(
      await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i),
      'https://makerworld.com/en/models/1400373',
    );
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    const sliceButtons = await screen.findAllByRole('button', {
      name: /Save & Slice in OrcaSlicer/,
    });
    expect(sliceButtons.length).toBe(2);
  });

  it('prefers the open_in_slicer override over preferred_slicer for the desktop handoff', async () => {
    // The URI-handoff label resolves via resolveDesktopSlicer: open_in_slicer
    // beats preferred_slicer (#1329) when the slicer API is off.
    useAuthedHandlers({ slicer: 'bambu_studio', openInSlicer: 'orcaslicer' });
    server.use(
      http.post('*/makerworld/resolve', () => HttpResponse.json(resolveResponse())),
    );
    render(<MakerworldPage />);
    await userEvent.type(
      await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i),
      'https://makerworld.com/en/models/1400373',
    );
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    const sliceButtons = await screen.findAllByRole('button', {
      name: /Save & Slice in OrcaSlicer/,
    });
    expect(sliceButtons.length).toBe(2);
  });

  it('keeps preferred_slicer for the in-app API slice label, ignoring open_in_slicer', async () => {
    // With the slicer API on, the sidecar drives the button, so the
    // open_in_slicer desktop override must not leak into the label.
    useAuthedHandlers({ slicer: 'bambu_studio', openInSlicer: 'orcaslicer', useSlicerApi: true });
    server.use(
      http.post('*/makerworld/resolve', () => HttpResponse.json(resolveResponse())),
    );
    render(<MakerworldPage />);
    await userEvent.type(
      await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i),
      'https://makerworld.com/en/models/1400373',
    );
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    const sliceButtons = await screen.findAllByRole('button', {
      name: /Save & Slice in Bambu Studio/,
    });
    expect(sliceButtons.length).toBe(2);
  });

  it('clears the resolved preview when the URL input is edited after resolve', async () => {
    useAuthedHandlers();
    server.use(
      http.post('*/makerworld/resolve', () => HttpResponse.json(resolveResponse())),
    );
    render(<MakerworldPage />);

    const input = await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i);
    await userEvent.type(input, 'https://makerworld.com/en/models/1400373');
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    expect(await screen.findByText('Seed Starter')).toBeInTheDocument();

    // User retypes — preview must go away so they can't accidentally submit the
    // previous model.
    await userEvent.clear(input);
    await userEvent.type(input, 'https://makerworld.com/en/models/9999999');

    await waitFor(() => {
      expect(screen.queryByText('Seed Starter')).not.toBeInTheDocument();
    });
  });

  it('renders inline imported-plate actions after a successful import', async () => {
    useAuthedHandlers();
    server.use(
      http.post('*/makerworld/resolve', () => HttpResponse.json(resolveResponse())),
      http.post('*/makerworld/import', () =>
        HttpResponse.json({
          library_file_id: 99,
          filename: 'benchy.3mf',
          folder_id: 7,
          profile_id: 298919107,
          was_existing: false,
        }),
      ),
    );
    render(<MakerworldPage />);

    await userEvent.type(
      await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i),
      'https://makerworld.com/en/models/1400373',
    );
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    const saveButtons = await screen.findAllByRole('button', { name: /^Save$/ });
    await userEvent.click(saveButtons[0]);

    // Inline post-import row for that plate shows the "View in File Manager"
    // and the two slicer open buttons.
    await screen.findByRole('button', { name: /View in File Manager/i });
    expect(screen.getByRole('button', { name: /Open in Bambu Studio/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Open in OrcaSlicer/i })).toBeInTheDocument();
  });

  it('renders the Recent imports sidebar when the query returns items', async () => {
    useAuthedHandlers({
      recent: [
        {
          library_file_id: 11,
          filename: 'first-import.3mf',
          folder_id: 2,
          thumbnail_path: null,
          source_url: 'https://makerworld.com/models/1#profileId-2',
          created_at: '2025-01-01T12:00:00',
        },
        {
          library_file_id: 12,
          filename: 'second-import.3mf',
          folder_id: 2,
          thumbnail_path: null,
          source_url: null,
          created_at: '2025-01-02T12:00:00',
        },
      ],
    });
    render(<MakerworldPage />);

    await screen.findByText(/Recent imports/i);
    expect(await screen.findByText('first-import.3mf')).toBeInTheDocument();
    expect(screen.getByText('second-import.3mf')).toBeInTheDocument();
  });

  it('omits the Recent imports sidebar when the feed is empty', async () => {
    // Defensive — the CardHeader uses the same translation as a potential
    // inline header; only the sidebar should surface "Recent imports".
    useAuthedHandlers({ recent: [] });
    render(<MakerworldPage />);

    await screen.findByRole('heading', { name: 'MakerWorld' });
    await waitFor(() => {
      expect(screen.queryByText(/Recent imports/i)).not.toBeInTheDocument();
    });
  });

  it('sanitises the design summary HTML via DOMPurify', async () => {
    // User-authored summaries are not trusted; a ``<script>`` tag must be
    // stripped before it lands in the DOM via dangerouslySetInnerHTML.
    useAuthedHandlers();
    server.use(
      http.post('*/makerworld/resolve', () =>
        HttpResponse.json(
          resolveResponse({
            design: {
              ...resolveResponse().design,
              summary:
                '<p>Clean prose <b>here</b></p><script>window.__pwned = true;</script>',
            },
          }),
        ),
      ),
    );
    render(<MakerworldPage />);
    await userEvent.type(
      await screen.findByPlaceholderText(/https:\/\/makerworld\.com/i),
      'https://makerworld.com/en/models/1400373',
    );
    await userEvent.click(screen.getByRole('button', { name: /Resolve/i }));

    await screen.findByText('Seed Starter');
    // Safe content survives.
    expect(screen.getByText(/Clean prose/i)).toBeInTheDocument();
    // Hostile content is stripped — DOMPurify drops <script> entirely, so the
    // side-effect assignment can't have run.
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
    // And no literal ``<script>`` text leaks into the document.
    expect(document.body.innerHTML).not.toContain('window.__pwned');
  });
});

describe('MakerworldPage /makerworld?url= deep link (print flow)', () => {
  const MODEL = 'https://makerworld.com/en/models/1400373#profileId-298919564';
  let printNotice: string | null = null;
  let printBlock: string | null = null;

  afterEach(() => {
    window.history.replaceState(null, '', '/');
    printNotice = null;
    printBlock = null;
  });

  function usePrintFlowHandlers(printBodies: Array<Record<string, unknown>>) {
    useAuthedHandlers();
    server.use(
      http.post('*/makerworld/resolve', () => HttpResponse.json(resolveResponse())),
      http.post('*/makerworld/print', async ({ request }) => {
        printBodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(
          { job_id: 41, status_url: '/api/v1/slice-jobs/41', printer_id: 3 },
          { status: 202 },
        );
      }),
      http.get('*/slice-jobs/41', () =>
        HttpResponse.json({
          job_id: 41,
          status: 'completed',
          kind: 'library_file',
          source_id: 7,
          source_name: 'seed.3mf',
          created_at: '',
          started_at: '',
          completed_at: '',
          progress: null,
          result: {
            library_file_id: 8,
            name: 'seed.gcode.3mf',
            print_time_seconds: 3600,
            filament_used_g: 12,
            filament_used_mm: 4000,
            used_embedded_settings: true,
            printer_id: 3,
            source_library_file_id: 7,
            profile_id: 298919564,
            notice: printNotice,
            process_preset: { source: 'standard', id: '0.20mm Standard @BBL X2D' },
            design_overrides: ['wall_loops'],
            printer_preset: { source: 'standard', id: 'Bambu Lab X2D 0.4 nozzle' },
            filament_presets: [{ source: 'standard', id: 'Bambu PLA Basic @BBL X2D' }],
            plates: [{ plate: 1, ams_mapping: [0], block: printBlock }],
          },
        }),
      ),
      http.get('*/library/files/8/plates', () =>
        HttpResponse.json({
          file_id: 8,
          filename: 'seed.gcode.3mf',
          is_multi_plate: false,
          plates: [
            {
              index: 1,
              name: null,
              objects: [],
              has_thumbnail: true,
              thumbnail_url: null,
              print_time_seconds: 3600,
              filament_used_grams: 12,
              bed_type: 'Textured PEI Plate',
              filaments: [{ slot_id: 1, type: 'PLA', color: '#FF0000', used_grams: 12, used_meters: 4 }],
            },
          ],
        }),
      ),
      http.get('*/printers/3/status', () => HttpResponse.json({ connected: true, ams: [], vt_tray: [] })),
      http.get('*/slicer/printer-models', () => HttpResponse.json({ 'Bambu Lab X2D': 'X2D' })),
      http.get('*/slicer/presets', () => {
        const std = (name: string) => ({ id: name, name, source: 'standard' });
        const empty = { printer: [], process: [], filament: [] };
        return HttpResponse.json({
          local: empty,
          cloud: empty,
          orca_cloud: empty,
          standard: {
            printer: [std('Bambu Lab X2D 0.4 nozzle')],
            process: [std('0.20mm Standard @BBL X2D')],
            filament: [std('Bambu PLA Basic @BBL X2D'), std('Bambu PETG HF @BBL X2D')],
          },
          cloud_status: 'ok',
          orca_cloud_status: 'ok',
        });
      }),
      http.get('*/slicer/preset-values', () =>
        HttpResponse.json({ resolved: true, reason: 'ok', values: { layer_height: '0.2', wall_loops: '2' } }),
      ),
      http.get('*/library/files/7/plates', () =>
        HttpResponse.json({
          file_id: 7,
          filename: 'seed.3mf',
          is_multi_plate: false,
          plates: [],
          design_overrides: [{ key: 'wall_loops', value: '4', printer_coupled: false, preset_defining: false }],
        }),
      ),
    );
  }

  it('starts the flow with the encoded URL, #profileId included', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    usePrintFlowHandlers(bodies);
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
    render(<MakerworldPage />);

    expect(await screen.findByRole('button', { name: 'Print' })).toBeInTheDocument();
    expect(bodies).toEqual([{ url: MODEL, printer_id: null }]);
    expect(await screen.findByText('Textured PEI Plate')).toBeInTheDocument();
    // The job id lands in the URL so returning from the G-code preview resumes.
    expect(window.location.search).toContain('job=41');
  });

  it('edits settings on the preview and re-slices with them', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    usePrintFlowHandlers(bodies);
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
    render(<MakerworldPage />);

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Edit settings' }));
    const layer = await screen.findByLabelText(/^Layer height/);
    await user.clear(layer);
    await user.type(layer, '0.28');
    await user.selectOptions(await screen.findByLabelText('Filament 1'), 'standard:Bambu PETG HF @BBL X2D');
    await user.click(screen.getByRole('button', { name: 'Save & re-slice' }));

    await screen.findByRole('button', { name: 'Print' });
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toMatchObject({
      url: MODEL,
      printer_id: 3,
      process_overrides: { layer_height: '0.28' },
      design_overrides: ['wall_loops'],
      filament_presets: [{ source: 'standard', id: 'Bambu PETG HF @BBL X2D' }],
    });
  });

  it('says so instead of waiting forever when the server no longer has the job', async () => {
    usePrintFlowHandlers([]);
    server.use(
      http.get('*/slice-jobs/99', () =>
        HttpResponse.json({ detail: 'Slice job not found or expired' }, { status: 404 }),
      ),
    );
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}&job=99`);
    render(<MakerworldPage />);

    expect(await screen.findByText(/no longer has this print job/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('opens the G-code preview inside the flow and comes back to the preview', async () => {
    usePrintFlowHandlers([]);
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
    render(<MakerworldPage />);

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'G-code preview' }));
    expect(screen.getByTestId('gcode-viewer')).toHaveTextContent('/library/files/8/gcode?plate=1');
    expect(window.location.pathname).toBe('/makerworld');
    await user.click(screen.getByRole('button', { name: 'Back to preview' }));
    expect(screen.queryByTestId('gcode-viewer')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Print' })).toBeInTheDocument();
  });

  it('in the Android app, closing goes back to the MakerWorld tab', async () => {
    const closeFlow = vi.fn();
    window.BambuddyHandyApp = { closeFlow };
    try {
      usePrintFlowHandlers([]);
      window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
      render(<MakerworldPage />);
      await screen.findByRole('button', { name: 'Print' });
      await userEvent.click(screen.getByRole('button', { name: 'Close' }));
      expect(closeFlow).toHaveBeenCalledOnce();
    } finally {
      delete window.BambuddyHandyApp;
    }
  });

  it("steps the app's Back button out of the flow one layer at a time", async () => {
    const closeFlow = vi.fn();
    window.BambuddyHandyApp = { closeFlow };
    try {
      usePrintFlowHandlers([]);
      window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
      render(<MakerworldPage />);
      const user = userEvent.setup();

      await user.click(await screen.findByRole('button', { name: 'G-code preview' }));
      expect(screen.getByTestId('gcode-viewer')).toBeInTheDocument();
      act(() => {
        expect(window.BambuddyHandyBack?.()).toBe(true);
      });
      expect(screen.queryByTestId('gcode-viewer')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Print' })).toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'Edit settings' }));
      await screen.findByRole('button', { name: 'Save & re-slice' });
      act(() => {
        window.BambuddyHandyBack?.();
      });
      expect(await screen.findByRole('button', { name: 'Print' })).toBeInTheDocument();
      expect(closeFlow).not.toHaveBeenCalled();

      act(() => {
        window.BambuddyHandyBack?.();
      });
      expect(closeFlow).toHaveBeenCalledOnce();
    } finally {
      delete window.BambuddyHandyApp;
    }
  });

  it('puts back a #profileId that an unencoded link left in the page hash', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    usePrintFlowHandlers(bodies);
    window.history.replaceState(null, '', `/makerworld?url=${MODEL}`);
    render(<MakerworldPage />);

    await screen.findByRole('button', { name: 'Print' });
    expect(bodies[0].url).toBe(MODEL);
  });

  it('works without a #profileId', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    usePrintFlowHandlers(bodies);
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent('https://makerworld.com/en/models/1400373')}`);
    render(<MakerworldPage />);

    await screen.findByRole('button', { name: 'Print' });
    expect(bodies[0].url).toBe('https://makerworld.com/en/models/1400373');
  });

  it('asks which printer when the backend needs one', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    usePrintFlowHandlers(bodies);
    server.use(
      http.post('*/makerworld/print', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        bodies.push(body);
        if (!body.printer_id) {
          return HttpResponse.json(
            { detail: { code: 'printer_required', printers: [{ id: 3, name: 'Office X1C', model: 'X1C' }, { id: 4, name: 'P1S', model: 'P1S' }] } },
            { status: 409 },
          );
        }
        return HttpResponse.json(
          { job_id: 41, status_url: '', printer_id: 3 },
          { status: 202 },
        );
      }),
    );
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
    render(<MakerworldPage />);

    await userEvent.click(await screen.findByRole('button', { name: /Office X1C/ }));
    await screen.findByRole('button', { name: 'Print' });
    expect(bodies.map((b) => b.printer_id)).toEqual([null, 3]);
  });

  it('says when the creator settings were not used', async () => {
    printNotice = 'creator_printer_settings_not_used';
    usePrintFlowHandlers([]);
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
    render(<MakerworldPage />);

    expect(await screen.findByText(/without their printer-specific ones/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Print' })).toBeInTheDocument();
  });

  it('hands an ambiguous AMS match to the Print dialog instead of printing', async () => {
    printBlock = 'filament_ambiguous';
    usePrintFlowHandlers([]);
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
    render(<MakerworldPage />);

    expect(await screen.findByRole('button', { name: 'Open Print dialog' })).toBeInTheDocument();
    expect(screen.getByText(/same material in another colour/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Print' })).not.toBeInTheDocument();
  });

  it('asks for a go-to print profile when the printer has none', async () => {
    usePrintFlowHandlers([]);
    server.use(
      http.post('*/makerworld/print', () =>
        HttpResponse.json(
          { detail: { code: 'goto_profile_missing', printer_id: 3, message: 'Set a go-to print profile' } },
          { status: 409 },
        ),
      ),
    );
    window.history.replaceState(null, '', `/makerworld?url=${encodeURIComponent(MODEL)}`);
    render(<MakerworldPage />);

    expect(await screen.findByRole('button', { name: 'Open printer settings' })).toBeInTheDocument();
    expect(screen.getByText(/no go-to print profile yet/)).toBeInTheDocument();
  });
});
