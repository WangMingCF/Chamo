import Foundation
import ImageIO
import UniformTypeIdentifiers
// Decode a single HEIC image using Apple's system codec; no third-party HEVC binary.
guard CommandLine.arguments.count==3 else{exit(2)}
let src=URL(fileURLWithPath:CommandLine.arguments[1]),dst=URL(fileURLWithPath:CommandLine.arguments[2])
guard let source=CGImageSourceCreateWithURL(src as CFURL,nil),CGImageSourceGetCount(source)==1 else{fputs("多帧或无法读取的 HEIC\n",stderr);exit(3)}
let props=CGImageSourceCopyPropertiesAtIndex(source,0,nil) as? [CFString:Any] ?? [:]
let w=props[kCGImagePropertyPixelWidth] as? Int ?? 0,h=props[kCGImagePropertyPixelHeight] as? Int ?? 0
guard w>0,h>0,Double(w)*Double(h)<=80_000_000,let image=CGImageSourceCreateImageAtIndex(source,0,nil),let destination=CGImageDestinationCreateWithURL(dst as CFURL,UTType.tiff.identifier as CFString,1,nil) else{fputs("HEIC 解码失败或超过像素预算\n",stderr);exit(4)}
CGImageDestinationAddImage(destination,image,props as CFDictionary)
guard CGImageDestinationFinalize(destination) else{exit(5)}
