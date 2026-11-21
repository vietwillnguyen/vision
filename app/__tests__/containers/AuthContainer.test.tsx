import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import React from 'react';

import { AuthContainer } from '../../src/containers/AuthContainer';

describe('AuthContainer', () => {
  it('validates locally before calling signIn', async () => {
    const signIn = jest.fn();
    await render(<AuthContainer signIn={signIn} />);
    await fireEvent.changeText(screen.getByTestId('email-input'), 'not-an-email');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'short');
    await fireEvent.press(screen.getByTestId('submit-button'));
    expect(signIn).not.toHaveBeenCalled();
    expect(screen.getByTestId('email-error')).toBeTruthy();
    expect(screen.getByTestId('password-error')).toBeTruthy();
  });

  it('calls signIn with valid credentials and surfaces auth errors', async () => {
    const signIn = jest.fn().mockResolvedValue('Invalid login credentials');
    await render(<AuthContainer signIn={signIn} />);
    await fireEvent.changeText(screen.getByTestId('email-input'), 'a@b.co');
    await fireEvent.changeText(screen.getByTestId('password-input'), 'password123');
    await fireEvent.press(screen.getByTestId('submit-button'));
    expect(signIn).toHaveBeenCalledWith('a@b.co', 'password123');
    await waitFor(() =>
      expect(screen.getByTestId('auth-error')).toHaveTextContent('Invalid login credentials'),
    );
  });

  it('hides the demo entry when the build carries no demo config', async () => {
    await render(<AuthContainer signIn={jest.fn()} demo={null} />);
    expect(screen.queryByTestId('view-demo-button')).toBeNull();
  });

  it('signs in as the demo account from public config without touching the form', async () => {
    const signIn = jest.fn().mockResolvedValue(null);
    await render(
      <AuthContainer signIn={signIn} demo={{ email: 'demo@example.com', password: 'pw' }} />,
    );
    expect(screen.getByText(/read-only/i)).toBeTruthy();
    await fireEvent.press(screen.getByTestId('view-demo-button'));
    expect(signIn).toHaveBeenCalledWith('demo@example.com', 'pw');
    expect(screen.queryByTestId('email-error')).toBeNull();
  });

  it('explains a demo that cannot be opened', async () => {
    const signIn = jest.fn().mockResolvedValue('Invalid login credentials');
    await render(
      <AuthContainer signIn={signIn} demo={{ email: 'demo@example.com', password: 'pw' }} />,
    );
    await fireEvent.press(screen.getByTestId('view-demo-button'));
    await waitFor(() =>
      expect(screen.getByTestId('auth-error')).toHaveTextContent(
        'The demo is unavailable right now (Invalid login credentials).',
      ),
    );
  });
});
