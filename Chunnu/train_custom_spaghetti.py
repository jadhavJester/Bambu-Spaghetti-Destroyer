#!/usr/bin/env python3
"""Custom YOLOv8 Training Script for 3D Printing Spaghetti Detection.

Usage:
1. Place annotated failure dataset into `dataset/` (with data.yaml, train/, val/).
2. Run `python train_custom_spaghetti.py`.
"""
from __future__ import annotations

import os
from ultralytics import YOLO


def train_spaghetti_model(
    data_yaml: str = "dataset/data.yaml",
    base_model: str = "yolov8n.pt",
    epochs: int = 30,
    img_size: int = 640,
    batch_size: int = 16,
):
    print(f"[*] Starting YOLO Training with base={base_model}, epochs={epochs}...")
    model = YOLO(base_model)
    
    # Train
    results = model.train(
        data=data_yaml,
        epochs=epochs,
        imgsz=img_size,
        batch=batch_size,
        name="bambu_spaghetti_detector",
        save=True,
    )
    
    print("[+] Training complete! Best weights saved to runs/detect/bambu_spaghetti_detector/weights/best.pt")
    return results


if __name__ == "__main__":
    if not os.path.exists("dataset/data.yaml"):
        print("[!] Note: To train a custom model, download a 3D print failure dataset")
        print("    (e.g., from Roboflow Universe '3d-printing-failure') into a folder named 'dataset/'")
        print("    Current active model is already running high-accuracy weights: 'spaghetti_yolo.pt'.")
    else:
        train_spaghetti_model()
