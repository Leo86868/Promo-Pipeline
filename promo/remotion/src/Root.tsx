import { Composition } from "remotion";
import {
  HotelPromo,
  calculateHotelPromoMetadata,
  hotelPromoSchema,
} from "./HotelPromo";

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="HotelPromo"
      component={HotelPromo}
      calculateMetadata={calculateHotelPromoMetadata}
      schema={hotelPromoSchema}
      width={1080}
      height={1920}
      defaultProps={{
        meta: {
          poiName: "Test Hotel",
          location: "Test City",
          fps: 30,
          width: 1080,
          height: 1920,
        },
        clips: [],
        audio: {
          bgmVolume: 0.35,
          bgmDuckedVolume: 0.18,
          duckRampSec: 0.3,
          pauseWindows: [],
        },
        captions: {
          wordTimestamps: [],
          highlightColor: "#D4AF37",
          defaultColor: "#FFFFFF",
          fontFamily: "Montserrat",
          fontSize: 48,
        },
        segments: [],
        anchor: {
          enabled: false,
          text: "BOOK HERE",
          startSec: 0,
          durationSec: 4,
        },
      }}
    />
  );
};
