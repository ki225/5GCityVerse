import { CityObject } from './model'
import type { ObjectConfig } from './model'

/** Individually selectable infrastructure; none of these objects are baked into a picture. */
export class CityLandmark extends CityObject {
  constructor(readonly landmarkType: 'railway' | 'train' | 'crane' | 'ship', config: ObjectConfig) {
    super(`landmark:${landmarkType}`, 'landscape', config)
  }
}
