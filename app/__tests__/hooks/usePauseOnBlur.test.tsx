import { NavigationContext } from '@react-navigation/native';
import { renderHook } from '@testing-library/react-native';
import React from 'react';

import { usePauseOnBlur } from '../../src/hooks/usePauseOnBlur';

describe('usePauseOnBlur', () => {
  it('pauses the player when its screen is left', async () => {
    const listeners: Record<string, () => void> = {};
    const navigation = {
      addListener: (event: string, listener: () => void) => {
        listeners[event] = listener;
        return () => delete listeners[event];
      },
    };
    const player = { pause: jest.fn() };
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <NavigationContext.Provider value={navigation as never}>
        {children}
      </NavigationContext.Provider>
    );

    await renderHook(() => usePauseOnBlur(player), { wrapper });
    listeners.blur();

    expect(player.pause).toHaveBeenCalledTimes(1);
  });

  it('does nothing outside a navigator', async () => {
    const player = { pause: jest.fn() };
    await renderHook(() => usePauseOnBlur(player));
    expect(player.pause).not.toHaveBeenCalled();
  });
});
