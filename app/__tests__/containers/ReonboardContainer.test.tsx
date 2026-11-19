import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import React from 'react';

import { PAIRING_POLL_MS, ReonboardContainer } from '../../src/containers/ReonboardContainer';
import type { VisionClient } from '../../src/lib/supabase';

jest.mock(
  'react-native-qrcode-svg',
  () =>
    function MockQrCode(props: { value: string }) {
      const { Text } = jest.requireActual('react-native');
      return <Text testID="qr-code">{props.value}</Text>;
    },
);

type Result<T> = { data: T; error: { message: string } | null };
type CodeRow = { code: string; expires_at: string };

const NOW = new Date('2026-11-19T12:00:00Z').getTime();
const IN_TEN_MINUTES = new Date(NOW + 10 * 60 * 1000).toISOString();

interface FakeOptions {
  // Each read of devices takes the next row set; the last one repeats.
  deviceReads?: Record<string, unknown>[][];
  rpc?: () => Promise<Result<CodeRow[] | null>>;
}

function fakeClient({ deviceReads = [[{ device_user_id: 'old-pendant' }]], rpc }: FakeOptions) {
  let reads = 0;
  const filters: [string, unknown][] = [];
  const rpcCalls: [string, unknown][] = [];
  const getSession = jest.fn();
  const chain = {
    select: () => chain,
    eq: (column: string, value: unknown) => {
      filters.push([column, value]);
      return chain;
    },
    order: () => chain,
    limit: () => {
      const rows = deviceReads[Math.min(reads, deviceReads.length - 1)];
      reads += 1;
      return Promise.resolve({ data: rows, error: null });
    },
  };
  let codes = 0;
  const client = {
    auth: { getSession },
    from: () => chain,
    rpc: (name: string, args: unknown) => {
      rpcCalls.push([name, args]);
      codes += 1;
      return rpc
        ? rpc()
        : Promise.resolve({
            data: [{ code: `code-${codes}`, expires_at: IN_TEN_MINUTES }],
            error: null,
          });
    },
  } as unknown as VisionClient;
  return { client, filters, rpcCalls, getSession, reads: () => reads };
}

async function generate() {
  await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
  await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2pass');
  await fireEvent.press(screen.getByTestId('generate-qr-button'));
}

function qrPayload() {
  return JSON.parse(screen.getByTestId('qr-code').props.children);
}

async function advance(ms: number) {
  await act(async () => {
    jest.advanceTimersByTime(ms);
  });
}

describe('ReonboardContainer', () => {
  beforeEach(() => {
    jest.useFakeTimers({ now: NOW });
  });
  afterEach(() => {
    // Real timers: this test parks the handler on a promise it settles by
    // hand inside act(), which does not mix with fake timers driving waitFor.
    jest.useRealTimers();
  });

  it('asks the database for a one-time code for this device, never for the login', async () => {
    const fake = fakeClient({});
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />);
    await generate();

    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    expect(fake.rpcCalls).toEqual([['create_pairing_code', { p_device_id: 'dev-1' }]]);
    expect(fake.getSession).not.toHaveBeenCalled();
  });

  it('puts exactly the v2 payload firmware parses in the QR', async () => {
    const fake = fakeClient({});
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />);
    await generate();

    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    expect(qrPayload()).toEqual({
      v: 2,
      ssid: 'HomeNet',
      password: 'hunter2pass',
      pairing_code: 'code-1',
    });
  });

  it('asks for a code that creates the device when the account has none', async () => {
    const fake = fakeClient({ deviceReads: [[]] });
    await render(<ReonboardContainer client={fake.client} deviceId={null} onDone={jest.fn()} />);
    await generate();

    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    expect(fake.rpcCalls).toEqual([['create_pairing_code', {}]]);
  });

  it('shows the error step when the code cannot be created', async () => {
    const fake = fakeClient({
      rpc: () => Promise.resolve({ data: null, error: { message: 'device not found' } }),
    });
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />);
    await generate();

    await waitFor(() =>
      expect(screen.getByTestId('reonboard-error')).toHaveTextContent('device not found'),
    );
  });

  it('shows the error step when the request itself fails', async () => {
    const fake = fakeClient({ rpc: () => Promise.reject(new Error('fetch failed')) });
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />);
    await generate();

    await waitFor(() =>
      expect(screen.getByTestId('reonboard-error')).toHaveTextContent('fetch failed'),
    );
  });

  it('shows Pendant paired once the device has a new pendant login', async () => {
    const fake = fakeClient({
      deviceReads: [
        [{ device_user_id: 'old-pendant' }],
        [{ device_user_id: 'old-pendant' }],
        [{ device_user_id: 'new-pendant' }],
      ],
    });
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />);
    await generate();
    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    expect(fake.filters).toContainEqual(['device_id', 'dev-1']);

    await advance(PAIRING_POLL_MS);
    expect(screen.queryByTestId('paired-message')).toBeNull();

    await advance(PAIRING_POLL_MS);
    await waitFor(() => expect(screen.getByTestId('paired-message')).toBeTruthy());

    // Polling stops once paired.
    const readsWhenPaired = fake.reads();
    await advance(PAIRING_POLL_MS * 3);
    expect(fake.reads()).toBe(readsWhenPaired);
  });

  it('shows Pendant paired once a first pairing creates the device', async () => {
    const fake = fakeClient({ deviceReads: [[], [{ device_id: 'dev-new' }]] });
    await render(<ReonboardContainer client={fake.client} deviceId={null} onDone={jest.fn()} />);
    await generate();
    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());

    await advance(PAIRING_POLL_MS);
    await waitFor(() => expect(screen.getByTestId('paired-message')).toBeTruthy());
  });

  it('stops polling when the screen goes away', async () => {
    const fake = fakeClient({});
    const { unmount } = await render(
      <ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />,
    );
    await generate();
    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    const readsBeforeUnmount = fake.reads();

    await unmount();
    await advance(PAIRING_POLL_MS * 3);

    expect(fake.reads()).toBe(readsBeforeUnmount);
  });

  it('expires the code and makes a new one on request', async () => {
    const fake = fakeClient({});
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />);
    await generate();
    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());

    await advance(10 * 60 * 1000);
    await waitFor(() => expect(screen.getByTestId('expired-message')).toBeTruthy());
    expect(screen.queryByTestId('qr-code')).toBeNull();

    await fireEvent.press(screen.getByTestId('new-code-button'));
    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    expect(qrPayload().pairing_code).toBe('code-2');
    expect(fake.rpcCalls).toHaveLength(2);
  });

  it('ignores a second press while the code is still being made', async () => {
    jest.useRealTimers();
    let respond!: (value: Result<CodeRow[]>) => void;
    const fake = fakeClient({ rpc: () => new Promise((resolve) => (respond = resolve)) });
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />);
    await fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2pass');
    // fireEvent resolves only once the async onPress handler settles, and this
    // one is parked on the rpc, so hold the first press open while the second
    // one lands on the now-disabled button.
    const firstPress = fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() =>
      expect(screen.getByTestId('generate-qr-button').props.accessibilityState.disabled).toBe(true),
    );
    await fireEvent.press(screen.getByTestId('generate-qr-button'));

    expect(fake.rpcCalls).toHaveLength(1);

    await act(async () => {
      respond({ data: [{ code: 'code-1', expires_at: IN_TEN_MINUTES }], error: null });
      await firstPress;
    });
    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
  });

  it('calls onDone from Done', async () => {
    const onDone = jest.fn();
    const fake = fakeClient({});
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={onDone} />);
    await generate();
    await waitFor(() => expect(screen.getByTestId('done-button')).toBeTruthy());

    await fireEvent.press(screen.getByTestId('done-button'));
    expect(onDone).toHaveBeenCalled();
  });

  it('keeps the typed SSID and password after Try again', async () => {
    const fake = fakeClient({
      rpc: () => Promise.resolve({ data: null, error: { message: 'boom' } }),
    });
    await render(<ReonboardContainer client={fake.client} deviceId="dev-1" onDone={jest.fn()} />);
    await generate();
    await waitFor(() => expect(screen.getByTestId('try-again-button')).toBeTruthy());

    await fireEvent.press(screen.getByTestId('try-again-button'));
    expect(screen.getByTestId('ssid-input').props.value).toBe('HomeNet');
    expect(screen.getByTestId('password-input').props.value).toBe('hunter2pass');
  });
});
