import { render, screen } from '@testing-library/react-native';
import React from 'react';
import { StyleSheet, Text } from 'react-native';

import { WebFrame } from '../../src/components/WebFrame';

const mockDimensions = { width: 0, height: 900, scale: 1, fontScale: 1 };
jest.mock('react-native/Libraries/Utilities/useWindowDimensions', () => ({
  __esModule: true,
  default: () => mockDimensions,
}));

function setWindowWidth(width: number) {
  mockDimensions.width = width;
}

describe('WebFrame', () => {
  it('is a pass-through on native', async () => {
    setWindowWidth(1280);
    await render(
      <WebFrame platform="ios">
        <Text>app</Text>
      </WebFrame>,
    );
    expect(screen.getByText('app')).toBeTruthy();
    expect(screen.queryByTestId('web-frame')).toBeNull();
  });

  it('centres a phone-width frame on a desktop browser', async () => {
    setWindowWidth(1280);
    await render(
      <WebFrame platform="web">
        <Text>app</Text>
      </WebFrame>,
    );
    const frame = StyleSheet.flatten(screen.getByTestId('web-frame').props.style);
    expect(frame.width).toBe(400);
    expect(frame.height).toBe(852);
  });

  it('fills a phone browser edge to edge', async () => {
    setWindowWidth(390);
    await render(
      <WebFrame platform="web">
        <Text>app</Text>
      </WebFrame>,
    );
    const frame = StyleSheet.flatten(screen.getByTestId('web-frame').props.style);
    expect(frame.width).toBeUndefined();
    expect(frame.flex).toBe(1);
  });
});
