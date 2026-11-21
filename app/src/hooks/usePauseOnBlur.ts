import { NavigationContext } from '@react-navigation/native';
import { useContext, useEffect } from 'react';

/**
 * Pause a player when its screen loses focus.
 *
 * Bottom tabs keep every visited screen mounted, so without this a reel kept
 * playing, sound and all, behind whichever tab the viewer moved to. Outside a
 * navigator (tests, a bare render) there is nothing to listen to.
 */
export function usePauseOnBlur(player: { pause: () => void } | null): void {
  const navigation = useContext(NavigationContext);

  useEffect(() => {
    if (!navigation || !player) return;
    return navigation.addListener('blur', () => player.pause());
  }, [navigation, player]);
}
